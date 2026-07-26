import os
from dataclasses import dataclass, field


@dataclass
class KibanaConfig:
    base_url: str = field(default_factory=lambda: os.environ.get("KIBANA_URL", "https://kibana.ext.prod.elk.cloudtrust.rocks").rstrip("/"))
    space_id: str = field(default_factory=lambda: os.environ.get("KIBANA_SPACE_ID", "gcs"))
    auth_method: str = field(default_factory=lambda: os.environ.get("KIBANA_AUTH_METHOD", "session"))
    api_key: str = field(default_factory=lambda: os.environ.get("KIBANA_API_KEY", ""))
    tls_verify: bool = field(default_factory=lambda: os.environ.get("KIBANA_TLS_VERIFY", "true").lower() not in ("false", "0", "no"))
    kbn_version: str = field(default_factory=lambda: os.environ.get("KBN_VERSION", "8.19.13"))
    kbn_build_number: str = field(default_factory=lambda: os.environ.get("KBN_BUILD_NUMBER", ""))
    username: str = field(default_factory=lambda: os.environ.get("KIBANA_USERNAME", ""))
    password: str = field(default_factory=lambda: os.environ.get("KIBANA_PASSWORD", ""))

    def api_base(self) -> str:
        """Returns /s/<space>/api for non-default spaces, else /api."""
        if self.space_id and self.space_id != "default":
            return f"/s/{self.space_id}/api"
        return "/api"


@dataclass
class OktaConfig:
    org: str = field(default_factory=lambda: os.environ.get("OKTA_ORG", "https://informatica.okta.com"))
    client_id: str = field(default_factory=lambda: os.environ.get("OKTA_CLIENT_ID", ""))


@dataclass
class AuthConfig:
    session_file: str = field(default_factory=lambda: os.environ.get("SESSION_FILE", ".kibana-session.json"))
    refresh_before_expiry_ms: int = 3 * 60 * 1000  # 3 minutes


@dataclass
class Config:
    kibana: KibanaConfig = field(default_factory=KibanaConfig)
    okta: OktaConfig = field(default_factory=OktaConfig)
    auth: AuthConfig = field(default_factory=AuthConfig)


config = Config()
