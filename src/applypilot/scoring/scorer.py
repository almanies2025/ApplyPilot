"""Job fit scoring: LLM-powered evaluation of candidate-job match quality.

Scores jobs on a 1-10 scale by comparing the user's resume against each
job description. All personal data is loaded at runtime from the user's
profile and resume file.
"""

import json
import logging
import re
import time
from datetime import datetime, timezone

from applypilot.config import RESUME_PATH, load_profile
from applypilot.database import get_connection, get_jobs_by_stage
from applypilot.llm import get_client

log = logging.getLogger(__name__)


# ── Scoring Prompt ────────────────────────────────────────────────────────

SCORE_PROMPT = """You are a job fit evaluator. Given a candidate's resume and a job description, score the candidate's realistic chance of being shortlisted for an interview.

SCORING CRITERIA:
- 9-10: Perfect match. Candidate has direct experience in nearly all required skills and qualifications.
- 7-8: Strong match. Candidate has most required skills, minor gaps easily bridged.
- 5-6: Moderate match. Candidate has some relevant skills but missing key requirements.
- 3-4: Weak match. Significant skill gaps, would need substantial ramp-up.
- 1-2: Poor match. Completely different field or experience level.

IMPORTANT FACTORS:
- Judge the candidate's WHOLE background, not only work history: the MBA (finance track, digital transformation, marketing), the first degree, languages (native Arabic, fluent English), international exposure, the AI/technology projects, and 4 years of management and operations experience in Saudi Arabia
- Judge the ACTUAL job from its description (duties, scope, requirements), never from the title alone. Executive, Associate, Analyst, Specialist, Consultant and Coordinator roles are as welcome as Manager roles when the duties suit an MBA graduate and the pay can support an Employment Pass (posted or likely pay of about SGD 6,000+ per month; if the description points to clerical, admin or support-level pay below that, score lower)
- Prefer Executive / Associate / Analyst / Specialist / Consultant roles: a role that manages a team or P&L, or needs 5+ years, is a stretch for this candidate (about 1 year of people management); score it lower than an equally good individual-contributor role (MANAGER ROLES ARE A STRETCH, see the candidate constraints)
- USER DECISION 2026-11-09 (flexible scoring): domain, industry, function, tools and a commercial-versus-finance flavour are NOT reasons to score low; treat any role whose duties an MBA graduate with 4 years of management and operations experience could learn on the job as at least a moderate match (role-fit part 3 or more of 4). Only salary and years of experience may lower the score, and only when clearly off: (1) required years: up to about 6 years asked is fine (no penalty), 7 years: minus 1, 8-9 years: at most 5, 10 or more: at most 3; 'X+ years preferred' is a soft ask and never a penalty. (2) pay: a posted pay maximum of SGD 5,400 per month or more is acceptable (within about 10% of the SGD 6,000 floor is 'a bit off', not a reason to reject); only a posted maximum clearly below SGD 5,400 caps the score at 4. Unposted pay is fine. Banks, insurers, asset managers and fintechs are treated like any other employer and need no more than the SGD 6,000 floor; their ladder titles (Associate, Senior Associate, Executive, Officer, Analyst, Assistant Vice President) are fine when the duties match what the candidate is looking for
- Consider transferable experience and the candidate's education and project work
- Be realistic about experience level vs. job requirements (years of experience, seniority)
- If CANDIDATE CONSTRAINTS are provided, apply them strictly; they override skill fit

RESPOND IN EXACTLY THIS FORMAT (no other text):
SCORE: [1-10]
KEYWORDS: [comma-separated ATS keywords from the job description that match or could match the candidate]
REASONING: [2-3 sentences explaining the score]"""


def _parse_score_response(response: str) -> dict:
    """Parse the LLM's score response into structured data.

    Args:
        response: Raw LLM response text.

    Returns:
        {"score": int, "keywords": str, "reasoning": str}
    """
    score = 0
    keywords = ""
    reasoning = response

    for line in response.split("\n"):
        line = line.strip()
        if line.startswith("SCORE:"):
            try:
                score = int(re.search(r"\d+", line).group())
                score = max(1, min(10, score))
            except (AttributeError, ValueError):
                score = 0
        elif line.startswith("KEYWORDS:"):
            keywords = line.replace("KEYWORDS:", "").strip()
        elif line.startswith("REASONING:"):
            reasoning = line.replace("REASONING:", "").strip()

    return {"score": score, "keywords": keywords, "reasoning": reasoning}


def _candidate_constraints(profile: dict) -> str:
    """Format the profile's job_preferences (seniority + hard constraints) for the scoring prompt."""
    prefs = profile.get("job_preferences", {})
    lines = []
    if prefs.get("seniority"):
        lines.append(f"- Target seniority: {prefs['seniority']}")
    lines.extend(f"- {c}" for c in prefs.get("constraints", []))
    return "CANDIDATE CONSTRAINTS:\n" + "\n".join(lines) if lines else ""


SALARY_CAP_SCORE = 4


def _max_monthly_salary(salary: str | None) -> float | None:
    """Best-effort parse of a posted salary into its maximum MONTHLY amount (None if unknown)."""
    if not salary:
        return None
    nums = [float(n.replace(",", "")) for n in re.findall(r"\d[\d,]*(?:\.\d+)?", salary)]
    nums = [n for n in nums if n >= 100]  # skip stray small numbers
    if not nums:
        return None
    s = salary.lower()
    top = max(nums)
    if "month" in s:
        return top
    if any(k in s for k in ("year", "annual", "annum", "/yr", "p.a")):
        return top / 12
    if "hour" in s:
        return top * 173
    return top / 12 if top > 30000 else top


def _apply_salary_floor(result: dict, job: dict, floor: float) -> dict:
    """Cap the score when the posting's maximum pay is below the candidate's minimum monthly salary."""
    top = _max_monthly_salary(job.get("salary"))
    # user 2026-11-09: 'a bit off' pay is fine, only cap when the posted maximum is clearly (>10%) below the floor
    if floor and top is not None and top < floor * 0.9 and result["score"] > SALARY_CAP_SCORE:
        result["score"] = SALARY_CAP_SCORE
        result["reasoning"] += (f" [Capped at {SALARY_CAP_SCORE}: posted salary max ~{top:,.0f}/month"
                                f" is below the candidate's minimum of {floor:,.0f}/month.]")
    return result


def score_job(resume_text: str, job: dict, constraints: str = "") -> dict:
    """Score a single job against the resume.

    Args:
        resume_text: The candidate's full resume text.
        job: Job dict with keys: title, site, location, full_description.
        constraints: Optional CANDIDATE CONSTRAINTS block from _candidate_constraints().

    Returns:
        {"score": int, "keywords": str, "reasoning": str}
    """
    job_text = (
        f"TITLE: {job['title']}\n"
        f"COMPANY: {job['site']}\n"
        f"LOCATION: {job.get('location', 'N/A')}\n\n"
        f"DESCRIPTION:\n{(job.get('full_description') or '')[:6000]}"
    )

    messages = [
        {"role": "system", "content": SCORE_PROMPT},
        {"role": "user", "content": f"RESUME:\n{resume_text}\n\n---\n\n"
                                    + (f"{constraints}\n\n---\n\n" if constraints else "")
                                    + f"JOB POSTING:\n{job_text}"},
    ]

    try:
        client = get_client()
        response = client.chat(messages, max_tokens=512, temperature=0.2)
        return _parse_score_response(response)
    except Exception as e:
        log.error("LLM error scoring job '%s': %s", job.get("title", "?"), e)
        return {"score": 0, "keywords": "", "reasoning": f"LLM error: {e}"}


def run_scoring(limit: int = 0, rescore: bool = False) -> dict:
    """Score unscored jobs that have full descriptions.

    Args:
        limit: Maximum number of jobs to score in this run.
        rescore: If True, re-score all jobs (not just unscored ones).

    Returns:
        {"scored": int, "errors": int, "elapsed": float, "distribution": list}
    """
    resume_text = RESUME_PATH.read_text(encoding="utf-8")
    profile = load_profile()
    constraints = _candidate_constraints(profile)
    salary_floor = float(profile.get("job_preferences", {}).get("min_monthly_salary") or 0)
    conn = get_connection()

    if rescore:
        query = "SELECT * FROM jobs WHERE full_description IS NOT NULL"
        if limit > 0:
            query += f" LIMIT {limit}"
        jobs = conn.execute(query).fetchall()
    else:
        jobs = get_jobs_by_stage(conn=conn, stage="pending_score", limit=limit)

    if not jobs:
        log.info("No unscored jobs with descriptions found.")
        return {"scored": 0, "errors": 0, "elapsed": 0.0, "distribution": []}

    # Convert sqlite3.Row to dicts if needed
    if jobs and not isinstance(jobs[0], dict):
        columns = jobs[0].keys()
        jobs = [dict(zip(columns, row)) for row in jobs]

    log.info("Scoring %d jobs sequentially...", len(jobs))
    t0 = time.time()
    completed = 0
    errors = 0
    results: list[dict] = []

    for job in jobs:
        result = _apply_salary_floor(score_job(resume_text, job, constraints), job, salary_floor)
        result["url"] = job["url"]
        completed += 1

        if result["score"] == 0:
            errors += 1

        results.append(result)

        # Save each score immediately so an interrupted run keeps its progress
        conn.execute(
            "UPDATE jobs SET fit_score = ?, score_reasoning = ?, scored_at = ? WHERE url = ?",
            (result["score"], f"{result['keywords']}\n{result['reasoning']}",
             datetime.now(timezone.utc).isoformat(), result["url"]),
        )
        conn.commit()

        log.info(
            "[%d/%d] score=%d  %s",
            completed, len(jobs), result["score"], job.get("title", "?")[:60],
        )

    elapsed = time.time() - t0
    log.info("Done: %d scored in %.1fs (%.1f jobs/sec)", len(results), elapsed, len(results) / elapsed if elapsed > 0 else 0)

    # Score distribution
    dist = conn.execute("""
        SELECT fit_score, COUNT(*) FROM jobs
        WHERE fit_score IS NOT NULL
        GROUP BY fit_score ORDER BY fit_score DESC
    """).fetchall()
    distribution = [(row[0], row[1]) for row in dist]

    return {
        "scored": len(results),
        "errors": errors,
        "elapsed": elapsed,
        "distribution": distribution,
    }
