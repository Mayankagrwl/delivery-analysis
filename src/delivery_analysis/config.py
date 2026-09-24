from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

DEFAULT_SRM_HOST = "https://trd.st.com"
SRM_RESOURCE_PATH = "resources/strn:distribution:DeliveryRequest"
SRM_ENVIRONMENTS = ("test", "int", "qa", "demo", "prod")
DEFAULT_SRM_ENV = "prod"
_PROD_ENV_ALIASES = {"prod", "production", ""}
DEFAULT_STATES = ("SUBMITTED", "GRANTED")
DEFAULT_STALE_HOURS = 24.0
DEFAULT_STALE_MODE = "any"
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_MCP_TRANSPORT = "sse"
DEFAULT_MCP_TOOL_TIMEOUT = 30.0
DEFAULT_DASHBOARD_UID = "d68f5a4d-72e6-4b16-b166-a70f41f3cd49"
DEFAULT_DASHBOARD_TITLE = "Service Logs"
DEFAULT_LINE_LIMIT = 200
DEFAULT_MAX_QUERIES = 32
LOGQL_TEMPLATES_PER_PACK = 8
DEFAULT_ENV = "production"
DEFAULT_COMPONENT = "distribution"
DEFAULT_LEVELS = "alert|error|warn"
DEFAULT_DASHBOARD_FILTERS = (
    f"env={DEFAULT_ENV},component={DEFAULT_COMPONENT},level={DEFAULT_LEVELS}"
)
STGPT_API_URL = "https://api-ai-bridge-dev.st.com/chatgpt/api/client-apps"
STGPT_CLIENT_APP_NAME = "gtrd_srmtdpplm"
STGPT_SERVICE = "chat"
STGPT_VERSION = "1.0"
PERSONAS = ("trinity_for_api", "alfred_for_api")
PROMPT_VERSION = "srm.s3.2"
TOKEN_BUDGET_TOTAL = 6000
STGPT_TIMEOUT_SECONDS = 60.0


def srm_url_for_env(env: str, *, host: str | None = None) -> str:
    """Build the SRM DeliveryRequest URL for a given environment.

    ``prod`` (also ``production`` / empty) uses the root resource path with no
    ``/distribution/<env>/`` segment; every other known env is nested under
    ``/distribution/<env>/``. Raises ``ValueError`` for an unknown env. The host
    defaults to ``DEFAULT_SRM_HOST`` and is overridable so it is never the only
    option. Env is normalized to lowercase/trimmed; a trailing ``/`` is stripped
    from the host.
    """
    resolved_host = (host or DEFAULT_SRM_HOST).strip().rstrip("/")
    normalized = (env or "").strip().lower()
    if normalized in _PROD_ENV_ALIASES:
        return f"{resolved_host}/{SRM_RESOURCE_PATH}"
    if normalized in SRM_ENVIRONMENTS:
        return f"{resolved_host}/distribution/{normalized}/{SRM_RESOURCE_PATH}"
    raise ValueError(
        f"unknown SRM env {env!r}; allowed: {', '.join(SRM_ENVIRONMENTS)}"
    )


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None:
        return default
    stripped = value.strip()
    return stripped if stripped else default


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def parse_dashboard_filters(raw: str | None) -> dict[str, str]:
    if not raw:
        return {}
    text = raw.strip()
    if not text:
        return {}
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return {}
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items() if v not in (None, "")}
        return {}
    out: dict[str, str] = {}
    for part in text.split(","):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        key = key.strip()
        value = value.strip()
        if key and value:
            out[key] = value
    return out


def _resolve_dashboard_filters(raw: str | None) -> dict[str, str]:
    merged = parse_dashboard_filters(DEFAULT_DASHBOARD_FILTERS)
    overrides = parse_dashboard_filters(raw)
    merged.update(overrides)
    return merged


def resolve_stgpt_api_key(explicit: str | None = None) -> str | None:
    """Resolve the ST ChatGPT bridge key. Never log it.

    Order: explicit argument, ``STGPT_API``, ``API_KEY``.
    """
    if explicit and explicit.strip():
        return explicit.strip()
    return _env("STGPT_API") or _env("API_KEY")


def resolve_stgpt_api_url(explicit: str | None = None) -> str:
    """Resolve the ST ChatGPT bridge base URL. Never append clientAppName."""
    if explicit and explicit.strip():
        return explicit.strip().rstrip("/")
    value = _env("STGPT_API_URL") or _env("API_URL")
    if value:
        return value.rstrip("/")
    return STGPT_API_URL.rstrip("/")


def resolve_stgpt_client_app_name(explicit: str | None = None) -> str:
    """Resolve clientAppName. Strip surrounding whitespace/newlines."""
    if explicit and explicit.strip():
        return explicit.strip()
    return _env("STGPT_CLIENT_APP_NAME") or _env("CLIENT_APP_NAME") or STGPT_CLIENT_APP_NAME


def tls_verify() -> bool | str:
    raw = _env("RCA_SSL_VERIFY", "true") or "true"
    if raw.lower() in {"0", "false", "no", "off"}:
        return False
    cert = _env("RCA_SSL_CERT_FILE") or _env("SSL_CERT_FILE")
    if cert:
        return cert
    return True


@dataclass
class Settings:
    srm_base_url: str
    srm_env: str
    srm_base_host: str
    srm_base_url_override: str | None
    request_id: str | None
    srm_basic_user: str | None
    srm_basic_password: str | None
    srm_states: tuple[str, ...]
    srm_timestamp_tz: str
    stale_hours: float
    stale_mode: str
    strict: bool
    tls_verify: bool | str
    timeout_seconds: float
    grafana_mcp_url: str | None = None
    grafana_mcp_token: str | None = None
    grafana_mcp_transport: str = DEFAULT_MCP_TRANSPORT
    grafana_loki_datasource_uid: str | None = None
    grafana_dashboard_uid: str = DEFAULT_DASHBOARD_UID
    grafana_dashboard_title: str = DEFAULT_DASHBOARD_TITLE
    grafana_dashboard_filters: dict[str, str] = field(default_factory=dict)
    loki_line_limit: int = DEFAULT_LINE_LIMIT
    loki_max_queries: int = DEFAULT_MAX_QUERIES
    include_debug_logs: bool = False
    query_grafana_on_empty: bool = False
    query_grafana_on_srm_error: bool = False
    mcp_tool_timeout: float = DEFAULT_MCP_TOOL_TIMEOUT
    stgpt_api_url: str = STGPT_API_URL
    stgpt_client_app_name: str = STGPT_CLIENT_APP_NAME
    token_budget: int = TOKEN_BUDGET_TOTAL
    stgpt_timeout: float = STGPT_TIMEOUT_SECONDS

    def __repr__(self) -> str:
        password = "***" if self.srm_basic_password else None
        token = "***" if self.grafana_mcp_token else None
        return (
            "Settings("
            f"srm_base_url={self.srm_base_url!r}, "
            f"srm_env={self.srm_env!r}, "
            f"srm_base_host={self.srm_base_host!r}, "
            f"request_id={self.request_id!r}, "
            f"srm_basic_user={self.srm_basic_user!r}, "
            f"srm_basic_password={password}, "
            f"srm_states={self.srm_states!r}, "
            f"srm_timestamp_tz={self.srm_timestamp_tz!r}, "
            f"stale_hours={self.stale_hours!r}, "
            f"stale_mode={self.stale_mode!r}, "
            f"strict={self.strict!r}, "
            f"tls_verify={self.tls_verify!r}, "
            f"timeout_seconds={self.timeout_seconds!r}, "
            f"grafana_mcp_url={self.grafana_mcp_url!r}, "
            f"grafana_mcp_token={token}, "
            f"grafana_mcp_transport={self.grafana_mcp_transport!r}, "
            f"grafana_dashboard_uid={self.grafana_dashboard_uid!r}, "
            f"stgpt_api_url={self.stgpt_api_url!r}, "
            f"stgpt_client_app_name={self.stgpt_client_app_name!r}, "
            f"token_budget={self.token_budget!r})"
        )


def load_settings(
    *,
    url: str | None = None,
    strict: bool | None = None,
    env: str | None = None,
    request_id: str | None = None,
) -> Settings:
    states_raw = _env("SRM_STATES", ",".join(DEFAULT_STATES)) or ",".join(DEFAULT_STATES)
    states = tuple(part.strip() for part in states_raw.split(",") if part.strip())
    stale_hours_raw = _env("STALE_HOURS", str(DEFAULT_STALE_HOURS)) or str(
        DEFAULT_STALE_HOURS
    )
    mode = (_env("STALE_MODE", DEFAULT_STALE_MODE) or DEFAULT_STALE_MODE).lower()
    if mode != DEFAULT_STALE_MODE:
        mode = DEFAULT_STALE_MODE
    resolved_strict = (
        _as_bool(_env("STRICT"), False) if strict is None else strict
    )
    transport = (
        _env("GRAFANA_MCP_TRANSPORT", DEFAULT_MCP_TRANSPORT) or DEFAULT_MCP_TRANSPORT
    ).lower()
    srm_base_host = (_env("SRM_BASE_HOST", DEFAULT_SRM_HOST) or DEFAULT_SRM_HOST).rstrip("/")
    srm_env = (env or _env("SRM_ENV", DEFAULT_SRM_ENV) or DEFAULT_SRM_ENV).strip().lower()
    # Full-URL escape hatch: explicit --url beats SRM_BASE_URL env.
    override = (url.strip() if url and url.strip() else None) or _env("SRM_BASE_URL")
    # Precedence: explicit --url > SRM_BASE_URL env > per-env computed URL.
    if override:
        resolved_srm_url = override
    elif srm_env in SRM_ENVIRONMENTS or srm_env in _PROD_ENV_ALIASES:
        resolved_srm_url = srm_url_for_env(srm_env, host=srm_base_host)
    else:
        # e.g. srm_env == "all": no single URL applies; keep a harmless prod
        # default. The multi-env orchestrator computes each env's URL itself.
        resolved_srm_url = srm_url_for_env(DEFAULT_SRM_ENV, host=srm_base_host)
    resolved_request_id = request_id if request_id is not None else _env("REQUEST_ID")
    if resolved_request_id is not None:
        resolved_request_id = resolved_request_id.strip() or None
    return Settings(
        srm_base_url=resolved_srm_url,
        srm_env=srm_env,
        srm_base_host=srm_base_host,
        srm_base_url_override=override,
        request_id=resolved_request_id,
        srm_basic_user=_env("SRM_BASIC_USER"),
        srm_basic_password=_env("SRM_BASIC_PASSWORD"),
        srm_states=states or DEFAULT_STATES,
        srm_timestamp_tz=(_env("SRM_TIMESTAMP_TZ", "UTC") or "UTC"),
        stale_hours=float(stale_hours_raw),
        stale_mode=mode,
        strict=resolved_strict,
        tls_verify=tls_verify(),
        timeout_seconds=DEFAULT_TIMEOUT_SECONDS,
        grafana_mcp_url=_env("GRAFANA_MCP_URL"),
        grafana_mcp_token=_env("GRAFANA_MCP_TOKEN"),
        grafana_mcp_transport=transport,
        grafana_loki_datasource_uid=_env("GRAFANA_LOKI_DATASOURCE_UID"),
        grafana_dashboard_uid=_env("GRAFANA_DASHBOARD_UID", DEFAULT_DASHBOARD_UID)
        or DEFAULT_DASHBOARD_UID,
        grafana_dashboard_title=_env("GRAFANA_DASHBOARD_TITLE", DEFAULT_DASHBOARD_TITLE)
        or DEFAULT_DASHBOARD_TITLE,
        grafana_dashboard_filters=_resolve_dashboard_filters(
            _env("GRAFANA_DASHBOARD_FILTERS")
        ),
        loki_line_limit=int(_env("LOKI_LINE_LIMIT", str(DEFAULT_LINE_LIMIT)) or DEFAULT_LINE_LIMIT),
        loki_max_queries=int(
            _env("LOKI_MAX_QUERIES", str(DEFAULT_MAX_QUERIES)) or DEFAULT_MAX_QUERIES
        ),
        include_debug_logs=_as_bool(_env("INCLUDE_DEBUG_LOGS"), False),
        query_grafana_on_empty=False,
        query_grafana_on_srm_error=_as_bool(_env("QUERY_GRAFANA_ON_SRM_ERROR"), False),
        mcp_tool_timeout=float(
            _env("MCP_TOOL_TIMEOUT", str(DEFAULT_MCP_TOOL_TIMEOUT)) or DEFAULT_MCP_TOOL_TIMEOUT
        ),
        stgpt_api_url=resolve_stgpt_api_url(),
        stgpt_client_app_name=resolve_stgpt_client_app_name(),
        token_budget=int(_env("TOKEN_BUDGET", str(TOKEN_BUDGET_TOTAL)) or TOKEN_BUDGET_TOTAL),
        stgpt_timeout=STGPT_TIMEOUT_SECONDS,
    )
