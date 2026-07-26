import json
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Optional, List

from kibana_mcp.config import config


@dataclass
class Session:
    cookie_name: str  # "sid" for Kibana 8.x SAML
    cookie_value: str
    expires_at: int  # Unix ms
    extra_headers: dict = field(default_factory=dict)
    okta_cookies: List[dict] = field(default_factory=list)


def load_session() -> Optional[Session]:
    try:
        if not os.path.exists(config.auth.session_file):
            return None
        with open(config.auth.session_file, "r") as f:
            data = json.load(f)
        return Session(
            cookie_name=data["cookieName"],
            cookie_value=data["cookieValue"],
            expires_at=data["expiresAt"],
            extra_headers=data.get("extraHeaders", {}),
            okta_cookies=data.get("oktaCookies", []),
        )
    except Exception as e:
        print(f"[auth] Failed to load session from {config.auth.session_file}: {e}", file=sys.stderr)
        return None


def save_session(session: Session) -> None:
    data = {
        "cookieName": session.cookie_name,
        "cookieValue": session.cookie_value,
        "expiresAt": session.expires_at,
        "extraHeaders": session.extra_headers,
        "oktaCookies": session.okta_cookies,
    }
    with open(config.auth.session_file, "w") as f:
        json.dump(data, f, indent=2)
    print(f"[auth] Session saved, expires at {_ms_to_iso(session.expires_at)}", file=sys.stderr)


def session_expires_in_ms(session: Session) -> int:
    return session.expires_at - int(time.time() * 1000)


def should_refresh(session: Session) -> bool:
    return session_expires_in_ms(session) < config.auth.refresh_before_expiry_ms


def _ms_to_iso(ms: int) -> str:
    import datetime
    return datetime.datetime.fromtimestamp(ms / 1000, tz=datetime.timezone.utc).isoformat()
