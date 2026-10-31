"""Local credential vault for employer-site accounts the agent creates.

Each employer gets its OWN unique random password (never the candidate's main password), stored in
~/.applypilot/accounts.json. The agent reports passwords with a line "ACCOUNT_PASSWORD: <site> | <email> | <password>".
"""
import json
import re
import secrets
import string
from datetime import datetime

from applypilot import config

PATH = config.APP_DIR / "accounts.json"
_TWO_TOKEN_PREFIXES = {"national", "international", "eastern", "singapore", "global", "asia", "the", "sea"}


def key(site: str | None) -> str:
    toks = re.findall(r"[a-z0-9]+", (site or "").lower())
    if not toks:
        return ""
    return "".join(toks[:2]) if toks[0] in _TWO_TOKEN_PREFIXES and len(toks) > 1 else toks[0]


def _load() -> dict:
    try:
        return json.loads(PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def get(site: str | None) -> dict | None:
    return _load().get(key(site))


def save(site: str | None, email: str, password: str) -> None:
    k = key(site)
    if not k or not password:
        return
    data = _load()
    data[k] = {"site": site, "email": email, "password": password, "saved": datetime.now().isoformat(timespec="seconds")}
    PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def new_password() -> str:
    """15 characters: upper, lower, digit and a symbol; fits the common 8-18 character rules."""
    letters = string.ascii_letters + string.digits
    chars = [secrets.choice(string.ascii_uppercase), secrets.choice(string.ascii_lowercase),
             secrets.choice(string.digits), secrets.choice("!#$%")]
    chars += [secrets.choice(letters) for _ in range(11)]
    secrets.SystemRandom().shuffle(chars)
    return "".join(chars)
