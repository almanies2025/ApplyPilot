"""Read-only inbox watcher: reads the newest message rows from the already signed-in Outlook tab (no clicks, no changes),
classifies each (confirmation / rejection / interview / action needed) and matches it to the application it belongs to.
Events go to ~/.applypilot/inbox_events.jsonl, which the live feed shows."""

from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.request
from datetime import datetime, timedelta, timezone

from applypilot import config
from applypilot.apply.outlook import is_outlook_url
from applypilot.database import get_connection

EVENTS = config.APP_DIR / "inbox_events.jsonl"
_LAST = config.APP_DIR / "inbox_last_scan"
MIN_INTERVAL_S = 240

# Rows of the message list: the accessible label carries "sender, subject, time, preview"
_JS = r"""(() => {
  const rows = Array.from(document.querySelectorAll('[role="option"], [data-convid]'));
  return JSON.stringify(rows.slice(0, 40).map(r => (r.getAttribute('aria-label') || r.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 400)).filter(Boolean));
})()"""

_KINDS = [  # first match wins, strongest signal first
    ("rejection", re.compile(r"unfortunately|not (?:be )?moving forward|will not be proceeding|regret to inform|other candidates|not selected|unsuccessful|no longer under consideration", re.I)),
    ("interview", re.compile(r"interview|invite you|schedule a (?:call|chat|time)|your availability|online (?:test|assessment)|hirevue|next steps|assessment", re.I)),
    ("action needed", re.compile(r"further action|action (?:is )?required|complete your|additional question|update your application", re.I)),
    ("confirmation", re.compile(r"thank you for (?:applying|your application|your interest)|thanks for applying|application (?:has been )?(?:received|submitted)|received your application|we(?:'ve| have) received|successfully (?:submitted|applied)|your application (?:for|to|was)", re.I)),
]
_SKIP_WORDS = {"pte", "ltd", "limited", "singapore", "group", "private", "inc", "corp", "the", "and", "asia", "pacific", "southeast",
               "company", "international", "holdings"}


def _cdp_eval(ws_url: str, expr: str, timeout: float = 15):
    import websocket

    ws = websocket.create_connection(ws_url, timeout=timeout)
    try:
        ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate", "params": {"expression": expr, "returnByValue": True}}))
        while True:
            msg = json.loads(ws.recv())
            if msg.get("id") == 1:
                return msg.get("result", {}).get("result", {}).get("value")
    finally:
        ws.close()


def _classify(text: str) -> str | None:
    for kind, rx in _KINDS:
        if rx.search(text):
            return kind
    return None


_GENERIC = _SKIP_WORDS | {"digital", "global", "solutions", "technology", "technologies", "services", "consulting", "management",
                          "systems", "capital", "partners", "network", "networks", "energy", "financial", "business", "health",
                          "research", "labs", "media", "data", "software", "advisory", "industries", "logistics", "trading"}
_PREFIX = re.compile(r"^(?:(?:unread|flagged|pinned|external sender|draft|replied|forwarded)\s+)+", re.I)


def _normalise(text: str) -> str:
    """Drop the status words Outlook puts in front of a row so the same email always gets the same id."""
    return _PREFIX.sub("", text.strip())


_ALIASES = {"procter": ["p&g", "pg"], "johnson": ["j&j", "jnj"], "pricewaterhouse": ["pwc"], "ernst": ["ey"], "ey": ["ernst"],
            "unilever": ["unilever"], "jollychic": ["jollychic"]}
_REGION = {"apac", "emea", "asean", "sg", "sea", "mena", "asia", "pacific"}


def _employer_tokens(site: str) -> list[str]:
    """The distinctive words of an employer name; generic and regional ones (digital, global, APAC...) never identify a company."""
    words = [w for w in re.split(r"[^a-z0-9]+", (site or "").lower()) if len(w) >= 2 and w not in _SKIP_WORDS and w not in _REGION]
    distinctive = [w for w in words if w not in _GENERIC and len(w) >= 3]
    return distinctive[:2]  # an employer with no distinctive word (e.g. "L.E.K. Consulting") is matched by job title only


def _compact(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", s.lower())


def _has_word(low: str, w: str) -> bool:
    return bool(re.search(rf"(?<![a-z0-9]){re.escape(w)}(?![a-z0-9])", low))


def _match(text: str, jobs: list[dict]) -> dict | None:
    """1) the job's exact title appears in the email (strongest); 2) every distinctive word of the employer appears."""
    low = text.lower()
    comp = _compact(text)
    best, best_score = None, 0
    for j in jobs:
        title_c = _compact(j["title"] or "")
        if len(title_c) >= 12 and title_c in comp:
            score = 100 + len(title_c)
            if score > best_score:
                best, best_score = j, score
    if best:
        return best
    for j in jobs:
        toks = _employer_tokens(j["site"])
        if not toks:
            continue
        hit = all(_has_word(low, t) or (len(t) >= 6 and _compact(t) in comp[:240]) or any(_has_word(low, a) for a in _ALIASES.get(t, [])) for t in toks)
        if not hit:
            continue
        title_words = {w for w in re.split(r"[^a-z0-9]+", (j["title"] or "").lower()) if len(w) >= 4}
        score = 1 + sum(1 for w in title_words if w in low)
        if score > best_score:
            best, best_score = j, score
    return best


def scan(port: int) -> list[dict]:
    """Read the Outlook tab once and record any new application emails. Returns the new events."""
    if _LAST.exists() and time.time() - _LAST.stat().st_mtime < MIN_INTERVAL_S:
        return []
    tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=10))
    tab = next((t for t in tabs if t.get("type") == "page" and is_outlook_url(t.get("url", "")) and "/mail" in t.get("url", "")), None)
    if not tab:
        return []
    raw = _cdp_eval(tab["webSocketDebuggerUrl"], _JS)
    _LAST.write_text(str(time.time()), encoding="utf-8")
    rows = json.loads(raw) if raw else []

    seen = set()
    if EVENTS.exists():
        for line in EVENTS.read_text(encoding="utf-8").splitlines():
            try:
                seen.add(json.loads(line)["id"])
            except (json.JSONDecodeError, KeyError):
                continue
    since = (datetime.now(timezone.utc) - timedelta(days=21)).isoformat()
    conn = get_connection()
    jobs = [dict(r) for r in conn.execute("SELECT url, title, site, applied_at FROM jobs WHERE applied_at >= ?", (since,))]

    new = []
    for text in rows:
        text = _normalise(text)
        kind = _classify(text)
        if not kind:
            continue
        eid = hashlib.sha1(re.sub(r"\d{1,2}:\d{2}\s?[AP]M", "", text[:200]).encode("utf-8", "ignore")).hexdigest()[:16]
        if eid in seen:
            continue
        seen.add(eid)
        job = _match(text, jobs)
        ev = {"id": eid, "t": int(time.time()), "kind": kind, "email": text[:220],
              "job": job["title"] if job else None, "employer": job["site"] if job else None, "url": job["url"] if job else None}
        new.append(ev)
    if new:
        with EVENTS.open("a", encoding="utf-8") as f:
            for ev in new:
                f.write(json.dumps(ev, ensure_ascii=False) + "\n")
    return new
