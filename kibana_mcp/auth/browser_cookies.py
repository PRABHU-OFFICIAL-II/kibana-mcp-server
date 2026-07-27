"""Read sid cookie directly from installed browser cookie stores.

On Windows, Chrome/Edge encrypt cookie values with DPAPI. The rookiepy library
handles decryption without requiring the browser to be open. This lets the server
reuse an existing browser Kibana session instead of triggering an Okta push.
"""

import platform
import sys
import time
from typing import Optional

KIBANA_HOST = "kibana.ext.prod.elk.cloudtrust.rocks"
KIBANA_COOKIE_NAME = "sid"
# Kibana SAML sessions typically last 1–8 hours; used as TTL for session cookies
_DEFAULT_TTL_MS = 1 * 60 * 60 * 1000


def try_read_browser_session() -> "Optional[Session]":
    """Try Chrome then Edge cookie stores; return a Session if a valid cookie is found.

    Returns None (never raises) so callers can safely fall through to the next
    auth step.
    """
    if platform.system() != "Windows":
        print("[auth] Browser cookie reading only supported on Windows — skipping", file=sys.stderr)
        return None

    try:
        import rookiepy
    except ImportError:
        print(
            "[auth] rookiepy not installed — skipping browser cookie read "
            "(run: pip install rookiepy)",
            file=sys.stderr,
        )
        return None

    browsers = [
        ("Chrome", getattr(rookiepy, "chrome", None)),
        ("Edge",   getattr(rookiepy, "edge", None)),
    ]

    for browser_name, browser_fn in browsers:
        if browser_fn is None:
            continue
        try:
            cookies = browser_fn([KIBANA_HOST])
            session = _find_kibana_session(cookies, browser_name)
            if session:
                return session
        except Exception as e:
            print(f"[auth] {browser_name}: cookie read error — {e}", file=sys.stderr)

    print("[auth] No valid sid cookie found in any browser cookie store", file=sys.stderr)
    return None


def _find_kibana_session(cookies: list, browser_name: str) -> "Optional[Session]":
    """Scan a list of rookiepy cookies for a non-expired sid cookie."""
    from kibana_mcp.auth.session import Session, save_session

    now_ms = int(time.time() * 1000)

    for c in cookies:
        if c.get("name") != KIBANA_COOKIE_NAME:
            continue

        value = c.get("value", "").strip()
        if not value:
            continue

        # rookiepy returns expires as Unix seconds; 0 / None = session cookie
        raw_exp = c.get("expires") or 0
        expires_ms = int(raw_exp) * 1000 if raw_exp else now_ms + _DEFAULT_TTL_MS

        if expires_ms <= now_ms:
            print(
                f"[auth] {browser_name}: sid cookie is expired — skipping",
                file=sys.stderr,
            )
            continue

        minutes_left = round((expires_ms - now_ms) / 60_000)
        print(
            f"[auth] {browser_name}: sid cookie found, valid for {minutes_left} min",
            file=sys.stderr,
        )

        session = Session(cookie_name="sid", cookie_value=value, expires_at=expires_ms)
        save_session(session)
        return session

    return None
