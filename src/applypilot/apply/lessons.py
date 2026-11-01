"""Cross-application memory for the apply agent: short, factual tips learned from earlier jobs (stored locally).

Self-maintaining: near-duplicate lessons are merged (and counted), time-limited ones (outages, "still down today") expire after a day,
and the store compacts itself when it grows. Reading returns the most useful distinct lessons: repeated ones first, then newest.
"""

from __future__ import annotations

import json
import re
import time

from applypilot import config

FILE = config.APP_DIR / "lessons.jsonl"
MAX_ROWS = 300
TRANSIENT_TTL = 24 * 3600
_URL = re.compile(r"https?://\S+|www\.\S+")
_WORD = re.compile(r"[a-z0-9]+")
_STOP = {"a", "an", "the", "to", "of", "and", "or", "is", "was", "on", "in", "for", "so", "it", "no", "there", "that", "this",
         "when", "if", "with", "as", "be", "at", "by", "then", "than", "but", "not"}
# Outages, dates and "still/again" notes describe one moment, not a lasting rule
_TRANSIENT = re.compile(
    r"\b(503|502|504|maintenance|outage|temporarily|currently down|is down|was down|still (?:on|returning|showing|down)|again|today|"
    r"\d{1,2} (?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w* 20\d\d|retry (?:on )?a later day|later day)\b", re.I)


_SUFFIX = re.compile(r"\b(pte|ltd|limited|inc|llc|llp|corp|corporation|co|company|singapore|sg|asia|apac|group|holdings|international|services)\b\.?", re.I)
# A lesson must be a plain observation about a site: never a secret, an instruction aimed at the agent, or a way round the rules.
_UNSAFE = re.compile(
    r"(\b(ignore|disregard|override|forget|bypass)\b.{0,40}\b(rule|rules|instruction|instructions|guardrail|prompt|check|captcha)\b"
    r"|system prompt|\bnew rule\b|\b(use|buy|rent|get)\b.{0,25}\bvirtual (phone )?number"
    r"|\bwork ?around\b.{0,30}\b(captcha|bot|security)"
    r"|\bpass(word|code)\b\s*(is|=|:)\s*\S+"
    r"|@(mba\.)?smu\.edu\.sg|\bAH\d{4,}\b|\bpassport (no|number)\b\s*(is|=|:)?\s*[A-Z0-9]{6,})", re.I)
# Absolute negatives ("can't be done", "no way in") describe one run, not the site: a later success must retire them
_NEGATIVE = re.compile(r"\b(no way|never (?:becomes|became|loads?)|cannot|can't|impossible|not possible|always fails|will not|won't work)\b", re.I)
N_GAP = 6 * 3600  # repeats of the same lesson inside this gap (retries of one job) count once


def norm_site(site: str) -> str:
    s = _SUFFIX.sub(" ", (site or "").lower())
    s = re.sub(r"[^a-z0-9* ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip() or (site or "").lower()


def supersede_on_success(site: str) -> int:
    """An application at this employer went through: drop its absolute-negative lessons (they were wrong or are now out of date)."""
    key = norm_site(site)
    rows = _rows()
    keep = [r for r in rows if not (norm_site(r.get("site", "")) == key and key != "*" and _NEGATIVE.search(r.get("text", "")))]
    if len(keep) != len(rows):
        _write(keep)
    return len(rows) - len(keep)


def _clean(text: str) -> str:
    text = _URL.sub("", text or "").replace("\n", " ").strip(" -:|`*\"'")
    return re.sub(r"\s+", " ", text)[:220]


def _words(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.lower()) if w not in _STOP}


def _similar(a: str, b: str, threshold: float = 0.4) -> bool:
    wa, wb = _words(a), _words(b)
    return bool(wa and wb) and len(wa & wb) / len(wa | wb) >= threshold


def _rows() -> list[dict]:
    if not FILE.exists():
        return []
    out = []
    for line in FILE.read_text(encoding="utf-8").splitlines():
        try:
            if line.strip():
                out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _write(rows: list[dict]) -> None:
    FILE.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")


def _alive(r: dict, now: float) -> bool:
    return not r.get("exp") or r["exp"] > now


def compact() -> int:
    """Drop expired lessons, merge near-duplicates (keeping their count), cap the store. Returns the new size."""
    now = time.time()
    kept: list[dict] = []
    for r in sorted(_rows(), key=lambda x: x.get("t", 0)):
        r["site"] = norm_site(r.get("site", ""))
        r["n"] = min(r.get("n", 1), 5)
        if _UNSAFE.search(r.get("text", "")):
            continue
        if "exp" not in r and _TRANSIENT.search(r.get("text", "")):  # older lessons saved before expiry existed
            r["exp"] = int(r.get("t", now) + TRANSIENT_TTL)
        if not _alive(r, now):
            continue
        twin = next((k for k in kept if k["site"] == r["site"] and _similar(k["text"], r["text"])), None)
        if twin:
            twin["n"] = twin.get("n", 1) + r.get("n", 1)
            twin["t"] = max(twin.get("t", 0), r.get("t", 0))
            if r.get("exp") and twin.get("exp"):
                twin["exp"] = max(twin["exp"], r["exp"])
        else:
            kept.append(r)
    kept.sort(key=lambda x: (x.get("n", 1), x.get("t", 0)), reverse=True)
    kept = kept[:MAX_ROWS]
    _write(kept)
    return len(kept)


def add(site: str, text: str, kind: str = "agent") -> None:
    text = _clean(text)
    if len(text) < 12 or _UNSAFE.search(text):
        return
    key = norm_site(site)
    now = time.time()
    rows = _rows()
    for r in rows:
        if norm_site(r.get("site", "")) == key and _similar(r.get("text", ""), text):  # the same lesson again: count it, refresh it
            if now - r.get("t", 0) > N_GAP:  # retries of one job are one observation, not several
                r["n"] = r.get("n", 1) + 1
            r["t"] = int(now)
            if r.get("exp"):
                r["exp"] = int(now + TRANSIENT_TTL)
            _write(rows)
            return
    row = {"t": int(now), "site": key, "kind": kind, "n": 1, "text": text}
    if _TRANSIENT.search(text):
        row["exp"] = int(now + TRANSIENT_TTL)
    rows.append(row)
    _write(rows)
    if len(rows) > MAX_ROWS * 1.3:
        compact()


def _best(rows: list[dict], limit: int) -> list[str]:
    now = time.time()
    out: list[str] = []
    for r in sorted((r for r in rows if _alive(r, now)), key=lambda x: (x.get("n", 1), x.get("t", 0)), reverse=True):
        if not any(_similar(r["text"], t) for t in out):
            out.append(r["text"])
        if len(out) >= limit:
            break
    return out


def relevant(site: str, limit_site: int = 6, limit_general: int = 6) -> tuple[list[str], list[str]]:
    """(tips for this employer/site, general tips): live, distinct, most-confirmed first."""
    rows = _rows()
    key = norm_site(site)
    return _best([r for r in rows if norm_site(r["site"]) == key], limit_site), _best([r for r in rows if r["site"] == "*"], limit_general)
