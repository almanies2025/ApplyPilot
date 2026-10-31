"""Text-to-PDF conversion for tailored resumes and cover letters.

Parses the structured text resume format, renders via an HTML/CSS template,
and exports to PDF using headless Chromium via Playwright.
"""

import logging
import re
from pathlib import Path

from applypilot.config import TAILORED_DIR

log = logging.getLogger(__name__)


# ── Resume Parser ────────────────────────────────────────────────────────

def parse_resume(text: str) -> dict:
    """Parse a structured text resume into sections.

    Expects a format with header lines (name, title, location, contact)
    followed by ALL-CAPS section headers (SUMMARY, TECHNICAL SKILLS, etc.).

    Args:
        text: Full resume text.

    Returns:
        {"name": str, "title": str, "location": str, "contact": str, "sections": dict}
    """
    lines = [line.rstrip() for line in text.strip().split("\n")]

    # Header: first few lines before SUMMARY
    header_lines: list[str] = []
    body_start = 0
    for i, line in enumerate(lines):
        if line.strip().upper() == "SUMMARY":
            body_start = i
            break
        if line.strip():
            header_lines.append(line.strip())

    name = header_lines[0] if len(header_lines) > 0 else ""
    title = header_lines[1] if len(header_lines) > 1 else ""
    # The header may have 3 or 4 lines depending on whether location is included
    location = ""
    contact = ""
    if len(header_lines) > 3:
        location = header_lines[2]
        contact = header_lines[3]
    elif len(header_lines) > 2:
        # Could be location or contact -- check for email/phone indicators
        if "@" in header_lines[2] or "|" in header_lines[2]:
            contact = header_lines[2]
        else:
            location = header_lines[2]

    # Split body into sections by ALL-CAPS headers
    sections: dict[str, str] = {}
    current_section: str | None = None
    current_lines: list[str] = []

    for line in lines[body_start:]:
        stripped = line.strip()
        # Detect section headers (all caps, no leading dash/bullet, longer than 3 chars)
        if (
            stripped
            and stripped == stripped.upper()
            and not stripped.startswith("-")
            and len(stripped) > 3
            and not stripped.startswith("\u2022")
        ):
            if current_section:
                sections[current_section] = "\n".join(current_lines).strip()
            current_section = stripped
            current_lines = []
        else:
            current_lines.append(line)

    if current_section:
        sections[current_section] = "\n".join(current_lines).strip()

    return {
        "name": name,
        "title": title,
        "location": location,
        "contact": contact,
        "sections": sections,
    }


def parse_skills(text: str) -> list[tuple[str, str]]:
    """Parse skills section into (category, value) pairs.

    Args:
        text: The TECHNICAL SKILLS section text.

    Returns:
        List of (category_name, skills_string) tuples.
    """
    skills: list[tuple[str, str]] = []
    for line in text.strip().split("\n"):
        line = line.strip()
        if ":" in line:
            cat, val = line.split(":", 1)
            skills.append((cat.strip(), val.strip()))
    return skills


def parse_entries(text: str) -> list[dict]:
    """Parse experience/project entries from section text.

    Args:
        text: The EXPERIENCE or PROJECTS section text.

    Returns:
        List of {"title": str, "subtitle": str, "bullets": list[str]} dicts.
    """
    entries: list[dict] = []
    lines = text.strip().split("\n")
    current: dict | None = None

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("- ") or stripped.startswith("\u2022 "):
            if current:
                current["bullets"].append(stripped[2:].strip())
        elif current is None or (
            not stripped.startswith("-")
            and not stripped.startswith("\u2022")
            and len(current.get("bullets", [])) > 0
        ):
            # New entry
            if current:
                entries.append(current)
            current = {"title": stripped, "subtitle": "", "bullets": []}
        elif current and not current["subtitle"]:
            current["subtitle"] = stripped
        else:
            if current:
                current["bullets"].append(stripped)

    if current:
        entries.append(current)

    return entries


# ── HTML Template ────────────────────────────────────────────────────────

def _edu_line_html(line: str) -> str:
    """Lay out one education line: "Degree | Institution | Dates | Location" or "Label: text"."""
    if " | " in line:
        parts = [p.strip() for p in line.split(" | ")]
        title, rest = parts[0], parts[1:]
        dates = next((p for p in rest if re.search(r"\b(19|20)\d{2}\b", p)), "")
        sub = " · ".join(p for p in rest if p != dates)
        return (f'<div class="edu-entry"><div class="edu-row"><span class="edu-title">{title}</span>'
                f'<span class="edu-date">{dates}</span></div>'
                + (f'<div class="edu-sub">{sub}</div>' if sub else "") + "</div>")
    if ":" in line:
        label, text = line.split(":", 1)
        return f'<div class="edu-note"><span class="skill-cat">{label}:</span>{text}</div>'
    return f'<div class="edu">{line}</div>'


def build_html(resume: dict) -> str:
    """Build professional resume HTML from parsed data.

    Args:
        resume: Parsed resume dict from parse_resume().

    Returns:
        Complete HTML string ready for PDF rendering.
    """
    sections = resume["sections"]

    # Skills
    skills_html = ""
    skills_key = next((k for k in ("SKILLS", "TECHNICAL SKILLS") if k in sections), None)
    if skills_key:
        skills = parse_skills(sections[skills_key])
        rows = ""
        for cat, val in skills:
            rows += f'<div class="skill-row"><span class="skill-cat">{cat}:</span> {val}</div>\n'
        skills_html = f'<div class="section"><div class="section-title">Skills</div>{rows}</div>'

    # Experience
    exp_html = ""
    if "EXPERIENCE" in sections:
        entries = parse_entries(sections["EXPERIENCE"])
        items = ""
        for e in entries:
            bullets = "".join(f"<li>{b}</li>" for b in e["bullets"])
            subtitle = f'<div class="entry-subtitle">{e["subtitle"]}</div>' if e["subtitle"] else ""
            items += f'<div class="entry"><div class="entry-title">{e["title"]}</div>{subtitle}<ul>{bullets}</ul></div>'
        exp_html = f'<div class="section"><div class="section-title">Experience</div>{items}</div>'

    # Projects
    proj_html = ""
    if "PROJECTS" in sections:
        entries = parse_entries(sections["PROJECTS"])
        items = ""
        for e in entries:
            bullets = "".join(f"<li>{b}</li>" for b in e["bullets"])
            subtitle = f'<div class="entry-subtitle">{e["subtitle"]}</div>' if e["subtitle"] else ""
            items += f'<div class="entry"><div class="entry-title">{e["title"]}</div>{subtitle}<ul>{bullets}</ul></div>'
        proj_html = f'<div class="section"><div class="section-title">Projects</div>{items}</div>'

    # Education
    edu_html = ""
    if "EDUCATION" in sections:
        edu_lines = "".join(_edu_line_html(line.strip())
                            for line in sections["EDUCATION"].splitlines() if line.strip())
        edu_html = f'<div class="section"><div class="section-title">Education</div>{edu_lines}</div>'

    # Summary
    summary_html = ""
    if "SUMMARY" in sections:
        summary_html = f'<div class="section"><div class="section-title">Summary</div><div class="summary">{sections["SUMMARY"].strip()}</div></div>'

    # Contact line parsing
    contact = resume["contact"]
    contact_parts = [p.strip() for p in contact.split("|")] if contact else []
    contact_html = " &nbsp;|&nbsp; ".join(contact_parts)

    # Location line (may be empty)
    location_html = f'<div class="location">{resume["location"]}</div>' if resume["location"] else ""

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
@page {{
    size: A4;
    margin: 0.35in 0.45in;
}}
* {{
    margin: 0;
    padding: 0;
    box-sizing: border-box;
}}
body {{
    font-family: 'Calibri', 'Segoe UI', Arial, sans-serif;
    font-size: 10pt;
    line-height: 1.3;
    color: #1a1a1a;
}}
.header {{
    text-align: center;
    margin-bottom: 4px;
    padding-bottom: 4px;
    border-bottom: 1.5px solid #2a7ab5;
}}
.name {{
    font-size: 18pt;
    font-weight: 700;
    color: #1a3a5c;
    letter-spacing: 0.5px;
}}
.title {{
    font-size: 10.5pt;
    color: #3a6b8c;
    margin: 1px 0;
}}
.location {{
    font-size: 9pt;
    color: #555;
}}
.contact {{
    font-size: 9pt;
    color: #444;
    margin-top: 1px;
}}
.contact a {{
    color: #2c3e50;
    text-decoration: none;
}}
.section {{
    margin-top: 10px;
}}
.summary, .skill-row, li, .edu {{
    text-align: justify;
}}
.section-title {{
    font-size: 10pt;
    font-weight: 700;
    color: #1a3a5c;
    text-transform: uppercase;
    letter-spacing: 0.8px;
    border-bottom: 1.5px solid #2a7ab5;
    padding-bottom: 1px;
    margin-bottom: 3px;
}}
.summary {{
    font-size: 9.5pt;
    color: #333;
    line-height: 1.4;
}}
.skill-row {{
    font-size: 9.5pt;
    margin: 0;
    line-height: 1.3;
}}
.skill-cat {{
    font-weight: 600;
    color: #1a3a5c;
}}
.entry {{
    margin-bottom: 4px;
    break-inside: avoid;
}}
.entry-title {{
    font-weight: 600;
    font-size: 10pt;
    color: #1a3a5c;
}}
.entry-subtitle {{
    font-size: 9pt;
    color: #4a7a9b;
    font-style: italic;
    margin-bottom: 1px;
}}
ul {{
    margin-left: 14px;
    padding: 0;
}}
li {{
    font-size: 9.5pt;
    margin-bottom: 1px;
    line-height: 1.3;
}}
.edu {{
    font-size: 9.5pt;
}}
.edu-entry {{
    margin-bottom: 3px;
}}
.edu-row {{
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    gap: 12px;
}}
.edu-title {{
    font-weight: 600;
    font-size: 9.5pt;
    color: #1a3a5c;
}}
.edu-date {{
    font-size: 9pt;
    font-style: italic;
    color: #4a7a9b;
    white-space: nowrap;
}}
.edu-sub {{
    font-size: 9pt;
    color: #444;
}}
.edu-note {{
    font-size: 9pt;
    margin: 0 0 4px 0;
    text-align: justify;
}}
</style>
</head>
<body>
<div class="header">
    <div class="name">{resume['name']}</div>
    <div class="title">{resume['title']}</div>
    {location_html}
    <div class="contact">{contact_html}</div>
</div>
{summary_html}
{skills_html}
{exp_html}
{proj_html}
{edu_html}
</body>
</html>"""


# ── PDF Renderer ─────────────────────────────────────────────────────────

FIT_SCALES = (1.0, 0.98, 0.96, 0.94, 0.92, 0.9)  # never shrink below 90% (keeps body text >= ~8.5pt)


def _pdf_page_count(pdf_bytes: bytes) -> int:
    return len(re.findall(rb"/Type\s*/Page(?!s)", pdf_bytes))


def render_pdf(html: str, output_path: str, fit_one_page: bool = False) -> None:
    """Render HTML to PDF using Playwright's headless Chromium.

    Args:
        html: Complete HTML string.
        output_path: Path to write the PDF file.
        fit_one_page: Scale down in small steps (to at most 90%) until the PDF fits on one page.
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.set_content(html, wait_until="networkidle")
        for scale in FIT_SCALES if fit_one_page else (1.0,):
            pdf_bytes = page.pdf(
                format="A4",
                margin={"top": "0", "right": "0", "bottom": "0", "left": "0"},
                print_background=True,
                scale=scale,
            )
            if _pdf_page_count(pdf_bytes) <= 1:
                break
        if fit_one_page and scale < 1.0:
            log.info("Scaled to %.0f%% to fit one page: %s", scale * 100, output_path)
        Path(output_path).write_bytes(pdf_bytes)
        browser.close()


# ── Cover letter template ────────────────────────────────────────────────

def build_letter_html(letter_text: str, personal: dict, re_line: str) -> str:
    """One-page A4 letter: name/contact header, Re: line, justified paragraphs."""
    import html as _html

    blocks = [b.strip() for b in letter_text.strip().split("\n\n") if b.strip()]
    salutation = blocks.pop(0) if blocks and blocks[0].lower().startswith("dear") else "Dear Hiring Manager,"
    close = blocks.pop() if blocks and len(blocks[-1].split()) <= 6 else ""
    linkedin = personal.get("linkedin_url", "").replace("https://www.", "").rstrip("/")
    contact = " &nbsp;·&nbsp; ".join(_html.escape(p) for p in (
        personal.get("phone", ""), personal.get("email", ""), linkedin, personal.get("city", "")) if p)
    paras = "\n".join(f"<p>{_html.escape(b)}</p>" for b in blocks)
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"><style>
@page {{ size: A4; margin: 0; }}
body {{ font-family: 'Times New Roman', Times, serif; font-size: 11.5pt; line-height: 1.42; color: #111;
        margin: 0; padding: 0.85in 0.95in 0.7in 0.95in; }}
.name {{ font-size: 18pt; font-weight: 700; letter-spacing: 1px; text-align: center; text-transform: uppercase; }}
.contact {{ font-size: 10pt; color: #333; text-align: center; margin-top: 3px; padding-bottom: 8px; border-bottom: 0.8px solid #555; }}
.re {{ margin-top: 22px; font-weight: 700; }}
.salutation {{ margin-top: 14px; }}
p {{ margin: 10px 0 0 0; text-align: justify; }}
.close {{ margin-top: 16px; white-space: pre-line; }}
</style></head><body>
<div class="name">{_html.escape(personal.get("full_name", ""))}</div>
<div class="contact">{contact}</div>
<div class="re">Re: {_html.escape(re_line)}</div>
<div class="salutation">{_html.escape(salutation)}</div>
{paras}
<div class="close">{_html.escape(close)}</div>
</body></html>"""


def render_cover_letter_pdf(text_path: Path, personal: dict, re_line: str) -> Path:
    """Render a cover letter .txt to a one-page letter PDF next to it."""
    text_path = Path(text_path)
    out = text_path.with_suffix(".pdf")
    render_pdf(build_letter_html(text_path.read_text(encoding="utf-8"), personal, re_line), str(out),
               fit_one_page=True)
    log.info("PDF generated: %s", out)
    return out


# ── Public API ───────────────────────────────────────────────────────────

def _trim_one_bullet(text: str) -> str | None:
    """Drop the single least-valuable bullet from a resume text (None if nothing can be trimmed).

    Order: the last bullet of the longest PROJECTS entry (keeping at least 1), then the last bullet of the
    longest EXPERIENCE entry (keeping at least 2). Bullets are ordered most-relevant-first, so the last goes first.
    """
    lines = text.split("\n")
    section, runs = None, []  # runs: (section, [line indexes of one consecutive bullet run])
    for i, line in enumerate(lines):
        s = line.strip()
        if s and s == s.upper() and not s.startswith("-") and len(s) > 3:
            section = s
        if line.startswith("- ") and section in ("PROJECTS", "EXPERIENCE"):
            if runs and runs[-1][0] == section and runs[-1][1][-1] == i - 1:
                runs[-1][1].append(i)
            else:
                runs.append((section, [i]))
    for sec, keep in (("PROJECTS", 1), ("EXPERIENCE", 2)):
        cands = [r for r in runs if r[0] == sec and len(r[1]) > keep]
        if cands:
            longest = max(len(r[1]) for r in cands)
            pick = [r for r in cands if len(r[1]) == longest][-1]
            del lines[pick[1][-1]]
            return "\n".join(lines)
    return None


def convert_to_pdf(
    text_path: Path, output_path: Path | None = None, html_only: bool = False
) -> Path:
    """Convert a text resume/cover letter to PDF.

    Args:
        text_path: Path to the .txt file to convert.
        output_path: Optional override for the output path. Defaults to same
            name with .pdf extension.
        html_only: If True, output HTML instead of PDF.

    Returns:
        Path to the generated PDF (or HTML) file.
    """
    text_path = Path(text_path)
    text = text_path.read_text(encoding="utf-8")
    resume = parse_resume(text)
    html = build_html(resume)

    if html_only:
        out = output_path or text_path.with_suffix(".html")
        out = Path(out)
        out.write_text(html, encoding="utf-8")
        log.info("HTML generated: %s", out)
        return out

    out = output_path or text_path.with_suffix(".pdf")
    out = Path(out)
    for _ in range(8):
        render_pdf(html, str(out), fit_one_page=True)
        if _pdf_page_count(out.read_bytes()) <= 1:
            break
        trimmed = _trim_one_bullet(text)  # still 2 pages at the smallest allowed scale: drop the weakest bullet
        if trimmed is None:
            break
        text = trimmed
        text_path.write_text(text, encoding="utf-8")
        html = build_html(parse_resume(text))
        log.info("Trimmed one low-priority bullet to fit one page: %s", out.name)
    log.info("PDF generated: %s", out)
    return out


def batch_convert(limit: int = 50) -> int:
    """Convert .txt files in TAILORED_DIR that don't have corresponding PDFs.

    Scans for .txt files (excluding _JOB.txt and _REPORT.json), checks if a
    .pdf with the same stem already exists, and converts any that are missing.

    Args:
        limit: Maximum number of files to convert.

    Returns:
        Number of PDFs generated.
    """
    if not TAILORED_DIR.exists():
        log.warning("Tailored directory does not exist: %s", TAILORED_DIR)
        return 0

    txt_files = sorted(TAILORED_DIR.glob("*.txt"))
    # Exclude _JOB.txt and _CL.txt files from resume conversion
    # (they get their own conversion calls)
    candidates = [
        f for f in txt_files
        if not f.name.endswith("_JOB.txt")
    ]

    # Filter to those without a corresponding PDF
    to_convert: list[Path] = []
    for f in candidates:
        pdf_path = f.with_suffix(".pdf")
        if not pdf_path.exists():
            to_convert.append(f)
        if len(to_convert) >= limit:
            break

    if not to_convert:
        log.info("All text files already have PDFs.")
        return 0

    log.info("Converting %d files to PDF...", len(to_convert))
    converted = 0
    for f in to_convert:
        try:
            convert_to_pdf(f)
            converted += 1
        except Exception as e:
            log.error("Failed to convert %s: %s", f.name, e)

    log.info("Done: %d/%d PDFs generated in %s", converted, len(to_convert), TAILORED_DIR)
    return converted
