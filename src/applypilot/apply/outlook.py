"""Keep the candidate's SMU Outlook mailbox signed in inside the agent's Chrome.

Called by the launcher before every job: if the mailbox session has expired it signs in again (email + password from the
OUTLOOK_PASSWORD environment variable, never stored on disk). It stops for good if Microsoft rejects the password
(marker file) so the account can never be locked by repeated attempts, and it does not handle phone/MFA prompts.
"""
import json
import logging
import time
import urllib.request

import websocket

from applypilot import config

logger = logging.getLogger(__name__)
BAD_PASSWORD_MARKER = config.APP_DIR / "OUTLOOK_BAD_PASSWORD"

_STATE = """(() => { const vis = e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
 const q = s => [...document.querySelectorAll(s)].filter(vis);
 return JSON.stringify({url: location.href, title: document.title,
  text: (document.body ? document.body.innerText : '').slice(0, 900).replace(/\\s+/g, ' '),
  pwd: q('input[type=password]').length,
  user: q('input[name=loginfmt], input[type=email], input[name=UserName], input[name=username], input[name=login]').length}); })()"""
_CLICK = """(() => { const vis = e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
 const pick = [...document.querySelectorAll('#idSIButton9, input[type=submit], button[type=submit]')].filter(vis)[0]
  || [...document.querySelectorAll('button, input[type=button]')].filter(vis).find(b => /^(next|sign in|yes|continue|submit|verify)$/i.test((b.innerText || b.value || '').trim()));
 if (!pick) return 'no button'; pick.click(); return 'clicked'; })()"""
_TICK_KMSI = "(() => { const c = document.querySelector('#KmsiCheckboxField'); if (c && !c.checked) c.click(); return 'ok'; })()"
_USER_SEL = "input[name=loginfmt], input[type=email], input[name=UserName], input[name=username], input[name=login]"


def _fill(sel: str, value: str) -> str:
    return f"""(() => {{ const vis = e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
 const els = [...document.querySelectorAll({json.dumps(sel)})].filter(vis); if (!els.length) return 'none';
 const el = els[0]; el.focus(); Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(el, {json.dumps(value)});
 el.dispatchEvent(new Event('input', {{bubbles: true}})); el.dispatchEvent(new Event('change', {{bubbles: true}})); return 'filled'; }})()"""


def is_outlook_url(url: str) -> bool:
    """The work/school mailbox (not personal outlook.live.com); Microsoft now redirects office.com to cloud.microsoft."""
    return any(d in (url or "") for d in ("outlook.office.com", "outlook.cloud.microsoft", "outlook.office365.com"))


def _http(port: int, path: str, method: str = "GET"):
    return urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method), timeout=20)


def ensure_signed_in(port: int, email: str, password: str, timeout: int = 75) -> str:
    """Return 'ok' (already signed in), 'signed_in' (signed in just now), 'mfa', 'bad_password' or 'failed'."""
    if BAD_PASSWORD_MARKER.exists():
        return "bad_password"
    pages = [t for t in json.load(_http(port, "/json")) if t.get("type") == "page"]
    tab = next((t for t in pages if is_outlook_url(t["url"]) or "login.microsoftonline.com" in t["url"]), None)
    if tab is None:
        tab = json.load(_http(port, "/json/new?https://outlook.office.com/mail/", "PUT"))
    ws = websocket.create_connection(tab["webSocketDebuggerUrl"], timeout=30)
    n = 0

    def js(expr: str):
        nonlocal n
        n += 1
        ws.send(json.dumps({"id": n, "method": "Runtime.evaluate", "params": {"expression": expr, "returnByValue": True}}))
        while True:
            m = json.loads(ws.recv())
            if m.get("id") == n:
                return m.get("result", {}).get("result", {}).get("value")

    acted, last_action = False, 0.0
    end = time.time() + timeout
    try:
        while time.time() < end:
            try:
                s = json.loads(js(_STATE))
            except Exception:
                time.sleep(2)
                continue
            low = s["text"].lower()
            if is_outlook_url(s["url"]) and "/mail" in s["url"] and "sign in" not in s["title"].lower() and not s["pwd"] and not s["user"]:
                return "signed_in" if acted else "ok"
            if "incorrect" in low and ("password" in low or "account" in low):
                BAD_PASSWORD_MARKER.write_text("Microsoft rejected the Outlook password; automatic sign-in is disabled.", encoding="utf-8")
                return "bad_password"
            if "stay signed in" in low:
                js(_TICK_KMSI)
                js(_CLICK)
                time.sleep(3)
                continue
            if any(k in low for k in ("approve sign in", "approve a request", "authenticator", "verification code", "enter code", "check your phone")):
                return "mfa"
            if time.time() - last_action > 12:
                if s["pwd"]:
                    if js(_fill("input[type=password]", password)) == "filled":
                        js(_CLICK)
                        acted, last_action = True, time.time()
                elif s["user"]:
                    if js(_fill(_USER_SEL, email)) == "filled":
                        js(_CLICK)
                        acted, last_action = True, time.time()
            time.sleep(2)
        return "failed"
    finally:
        try:
            ws.close()
        except Exception:
            pass
