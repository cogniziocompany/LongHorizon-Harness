from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    tomllib = __import__("tomllib")
except ModuleNotFoundError:
    tomllib = __import__("tomli")  # type: ignore[no-redef]

# The MCP gateway is the cognizioware LiteLLM MCP endpoint.  The public prod
# URL is the default; the LAN alias is available for on-prem deployments.
# The trailing slash is mandatory for the Streamable-HTTP gateway.
MCP_PROD_GATEWAY_URL = "https://litellm-gateway-api.cognizioware.com/mcp/"
MCP_LAN_GATEWAY_URL = "http://192.168.21.161:4000/mcp/"

_MCP_USER_PROFILES_FILE = "mcp_profiles.json"

# Built-in profile aliases.  These are the gateway's real `general_settings.mcp_aliases`
# (cognizioware-mcp-tools infrastructure/litellm-config.yaml, verified 2026-09-07) — dash-free
# on purpose (LiteLLM splits tool names on the first dash) — plus `langfuse_mcp`, which has no
# alias.  The gateway cannot scope a server to read-only; "audit" therefore lists only servers
# whose tools are inherently non-mutating (kb, guides, skills, memory, langfuse).
_BUILTINS: dict[str, dict[str, Any]] = {
    "none": {
        "description": "No MCP servers; disables generated per-role MCP config.",
        "servers": (),
        "read_only": True,
    },
    "audit": {
        "description": "Read-only audit tooling: kb, gateway guides/skills, hivemind memory, langfuse.",
        "servers": ("kb", "guides", "skills", "memory", "langfuse_mcp"),
        "read_only": True,
    },
    "default": {
        "description": "Audit + github, youtrack, ssh.",
        "servers": (
            "kb",
            "guides",
            "skills",
            "memory",
            "langfuse_mcp",
            "github",
            "youtrack",
            "ssh",
        ),
        "read_only": False,
    },
    "ops": {
        "description": "Default + hydra/fleet + proxmox hosts (pct/docker control).",
        "servers": (
            "kb",
            "guides",
            "skills",
            "memory",
            "langfuse_mcp",
            "github",
            "youtrack",
            "ssh",
            "hydra",
            "hydrafleet",
            "proxmoxptait01",
            "proxmoxptait07",
            "proxmoxcorsairai300",
        ),
        "read_only": False,
    },
    "full": {
        "description": "No gateway allow-list header; all configured servers permitted.",
        "servers": None,
        "read_only": False,
    },
}


@dataclass(frozen=True)
class McpProfile:
    """A resolved MCP profile for one run/role combination."""

    name: str
    description: str
    servers: tuple[str, ...] | None
    read_only: bool
    source: str
    reason: str = ""

    def is_none(self) -> bool:
        return self.name == "none" or self.servers == ()


def _state_root() -> Path:
    """Return the harness state root, defaulting to ~/.lh-harness."""
    env_root = os.environ.get("LH_HARNESS_STATE_ROOT")
    if env_root:
        return Path(env_root).expanduser().resolve()
    return Path.home() / ".lh-harness"


def _normalise_gateway_url(url: str | None) -> str:
    """Resolve gateway URL aliases and enforce a trailing slash."""
    if url is None:
        return MCP_PROD_GATEWAY_URL
    text = str(url).strip()
    if not text:
        return MCP_PROD_GATEWAY_URL
    if text == "lan":
        return MCP_LAN_GATEWAY_URL
    if not text.endswith("/"):
        text += "/"
    return text


def _gateway_key() -> str | None:
    """Return the configured gateway key, or None if missing."""
    value = os.environ.get("LH_HARNESS_MCP_GATEWAY_KEY")
    if value is None:
        return None
    text = str(value).strip()
    return text if text else None


def _extra_gateway_headers() -> dict[str, str]:
    """Parse optional extra headers from LH_HARNESS_MCP_GATEWAY_HEADERS_JSON."""
    raw = os.environ.get("LH_HARNESS_MCP_GATEWAY_HEADERS_JSON")
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {
        str(key).strip(): str(value)
        for key, value in parsed.items()
        if isinstance(key, str) and isinstance(value, (str, int, float, bool))
    }


def _builtin_profile(name: str, *, reason: str = "built-in") -> McpProfile:
    spec = _BUILTINS[name]
    return McpProfile(
        name=name,
        description=spec["description"],
        servers=tuple(spec["servers"]) if spec["servers"] is not None else None,
        read_only=spec["read_only"],
        source="built-in",
        reason=reason,
    )


def _load_user_profiles() -> dict[str, McpProfile]:
    """Load user-level profiles from ~/.lh-harness/mcp_profiles.json."""
    path = _state_root() / _MCP_USER_PROFILES_FILE
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, TypeError):
        return {}
    if not isinstance(data, dict):
        return {}
    profiles: dict[str, McpProfile] = {}
    for name, spec in data.items():
        if not isinstance(spec, dict) or not isinstance(name, str):
            continue
        servers = spec.get("servers")
        if servers is None:
            pass
        elif isinstance(servers, str):
            servers = tuple(part.strip() for part in servers.split(",") if part.strip())
        elif isinstance(servers, list):
            servers = tuple(str(item).strip() for item in servers if str(item).strip())
        else:
            continue
        profiles[name] = McpProfile(
            name=name,
            description=str(spec.get("description", "")),
            servers=servers,
            read_only=bool(spec.get("read_only", False)),
            source="user",
        )
    return profiles


def _load_project_profiles(path: Path | str | None) -> dict[str, McpProfile]:
    """Load project-level profiles from .lh-harness/config.toml [run.mcp_profiles.<name>]."""
    if path is None:
        path = Path(".lh-harness/config.toml")
    source = Path(path)
    if not source.is_file():
        return {}
    try:
        with source.open("rb") as fh:
            payload = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    run = payload.get("run", {})
    if not isinstance(run, dict):
        return {}
    raw_profiles = run.get("mcp_profiles", {})
    if not isinstance(raw_profiles, dict):
        return {}
    profiles: dict[str, McpProfile] = {}
    for name, spec in raw_profiles.items():
        if not isinstance(spec, dict) or not isinstance(name, str):
            continue
        servers = spec.get("servers")
        if servers is None:
            pass
        elif isinstance(servers, str):
            servers = tuple(part.strip() for part in servers.split(",") if part.strip())
        elif isinstance(servers, list):
            servers = tuple(str(item).strip() for item in servers if str(item).strip())
        else:
            continue
        profiles[name] = McpProfile(
            name=name,
            description=str(spec.get("description", "")),
            servers=servers,
            read_only=bool(spec.get("read_only", False)),
            source="project",
        )
    return profiles


def _web_default_profile(role: str | None = None) -> str | None:
    """Read a web-default env profile name.

    Web-default envs are exposed for the public role names (manager, executor,
    auditor), not the internal sub-roles.
    """
    if role:
        public_role = _public_role(role)
        env_name = f"LH_HARNESS_WEB_DEFAULT_{public_role.upper()}_MCP_PROFILE"
        value = os.environ.get(env_name)
        if value:
            return value.strip() or None
    value = os.environ.get("LH_HARNESS_WEB_DEFAULT_MCP_PROFILE")
    return value.strip() if value else None


def _public_role(role: str) -> str:
    if role in {"gui_executor", "cli_executor"}:
        return "executor"
    if role in {"gui_auditor", "cli_auditor", "auditor_format_repair"}:
        return "auditor"
    if role == "final_response":
        return "manager"
    return role


def _default_profile_for_role(role: str) -> str:
    from .adapters.claude_permissions import is_auditor_role

    if role == "auditor":
        return "audit"
    if is_auditor_role(role):
        return "audit"
    return "default"


def list_available_profiles(
    project_config_path: Path | str | None = None,
) -> list[dict[str, Any]]:
    """Return all profile names visible on this node for GET /api/meta.

    The result never includes the gateway key.
    """
    builtin = [
        {
            "name": name,
            "description": spec["description"],
            "read_only": spec["read_only"],
            "source": "built-in",
        }
        for name, spec in _BUILTINS.items()
    ]
    user = _load_user_profiles()
    project = _load_project_profiles(project_config_path)
    seen = set(_BUILTINS)
    result = list(builtin)
    for source_label, profiles in (("user", user), ("project", project)):
        for name, profile in profiles.items():
            if name in seen:
                continue
            seen.add(name)
            result.append(
                {
                    "name": name,
                    "description": profile.description,
                    "read_only": profile.read_only,
                    "source": source_label,
                }
            )
    return result


def resolve_profile(
    role: str,
    *,
    run_profile: str | None = None,
    role_profile: str | None = None,
    config: dict[str, Any] | None = None,
    project_config_path: Path | str | None = None,
    allow_auditor_write_mcp: bool = False,
    gateway_key: str | None = None,
) -> McpProfile:
    """Resolve the effective MCP profile for a role using the precedence rules.

    Precedence (highest first):
      1. API/CLI role override (role_profile)
      2. API/CLI run-level override (run_profile)
      3. project config [run.roles.<role>] mcp_profile
      4. project config run.mcp_profile
      5. web-defaults env (per-role, then global)
      6. built-in defaults (manager/executor -> default; auditor -> audit)

    If LH_HARNESS_MCP_GATEWAY_KEY is missing, every non-none profile is
    coerced to "none" with reason "gateway key not configured".
    """
    config = config or {}
    user_profiles = _load_user_profiles()
    project_profiles = _load_project_profiles(project_config_path)
    all_profiles: dict[str, McpProfile] = {}
    for name in _BUILTINS:
        all_profiles[name] = _builtin_profile(name)
    for source, profiles in (("user", user_profiles), ("project", project_profiles)):
        for name, profile in profiles.items():
            all_profiles.setdefault(name, profile)

    candidates: list[tuple[str, str]] = []
    if role_profile:
        candidates.append((role_profile, f"API/CLI role override for {role}"))
    if run_profile:
        candidates.append((run_profile, "API/CLI run-level override"))

    roles_config = config.get("roles", {}) if isinstance(config, dict) else {}
    if isinstance(roles_config, dict):
        role_config = roles_config.get(role, {})
        if isinstance(role_config, dict) and role_config.get("mcp_profile"):
            candidates.append((str(role_config["mcp_profile"]), f"config [run.roles.{role}]"))
    if config.get("mcp_profile"):
        candidates.append((str(config["mcp_profile"]), "config run.mcp_profile"))

    # Per-role web-default takes precedence over global web-default.
    public_role = _public_role(role)
    env_default = _web_default_profile(role)
    global_default = _web_default_profile()
    if env_default:
        # If env_default came from the per-role env, say so; otherwise it came
        # from the global env.
        per_role_env_name = f"LH_HARNESS_WEB_DEFAULT_{public_role.upper()}_MCP_PROFILE"
        if os.environ.get(per_role_env_name):
            env_reason = f"env {per_role_env_name}"
        else:
            env_reason = "env LH_HARNESS_WEB_DEFAULT_MCP_PROFILE"
        candidates.append((env_default, env_reason))
    elif global_default:
        candidates.append((global_default, "env LH_HARNESS_WEB_DEFAULT_MCP_PROFILE"))

    default_name = _default_profile_for_role(role)
    candidates.append((default_name, "built-in default"))

    requested_name: str | None = None
    resolved_name: str | None = None
    reason: str = ""
    for name, src in candidates:
        if requested_name is None:
            requested_name = name
        if name in all_profiles:
            resolved_name = name
            reason = src
            break

    if resolved_name is None:
        if requested_name is not None:
            # An explicit candidate was requested but is not a known profile.
            fallback = _default_profile_for_role(role)
            resolved_name = fallback
            reason = f"unknown profile {requested_name!r}; falling back to built-in {fallback}"
        else:
            resolved_name = "none"
            reason = "no matching profile found"
    elif requested_name != resolved_name:
        # A higher-precedence candidate was unknown; the next candidate won.
        reason = f"unknown profile {requested_name!r}; falling back to {resolved_name} ({reason})"

    profile = all_profiles.get(resolved_name)
    if profile is None:
        profile = _builtin_profile(resolved_name)

    # If gateway key is missing, non-none profiles become none.
    effective_key = gateway_key if gateway_key is not None else _gateway_key()
    if not profile.is_none() and effective_key is None:
        return McpProfile(
            name="none",
            description=_BUILTINS["none"]["description"],
            servers=(),
            read_only=True,
            source="built-in",
            reason="gateway key not configured",
        )

    # Auditor roles must use read-only profiles unless explicitly allowed.
    from .adapters.claude_permissions import is_auditor_role

    if is_auditor_role(role) and not profile.read_only and not allow_auditor_write_mcp:
        raise ValueError(
            f"MCP profile {profile.name!r} is not read-only; auditor roles require a read-only profile. "
            "Set a read-only profile or set config allow_auditor_write_mcp = true."
        )

    return McpProfile(
        name=profile.name,
        description=profile.description,
        servers=profile.servers,
        read_only=profile.read_only,
        source=profile.source,
        reason=reason,
    )


def gateway_configured() -> bool:
    """Return whether a gateway key is configured."""
    return _gateway_key() is not None


def render_mcp_config(
    profile: McpProfile,
    *,
    run_id: str,
    role: str,
    run_dir: str | Path,
    session_id: str,
    gateway_key: str | None = None,
) -> Path | None:
    """Render a per-role MCP config under <run_dir>/harness/mcp/<role>.mcp.json.

    Returns the generated path, or None for the "none" profile.  The file is
    written with mode 0600.  The Authorization header value is never logged.
    """
    if profile.is_none():
        return None

    key = gateway_key if gateway_key is not None else _gateway_key()
    if key is None:
        raise RuntimeError("cannot render MCP config without LH_HARNESS_MCP_GATEWAY_KEY")

    url = _normalise_gateway_url(os.environ.get("LH_HARNESS_MCP_GATEWAY_URL"))
    servers_header = ",".join(profile.servers) if profile.servers is not None else ""

    headers: dict[str, str] = {
        "Authorization": f"Bearer {key}",
        "x-mcp-servers": servers_header,
        "X-LH-Session": session_id,
    }
    headers.update(_extra_gateway_headers())

    data = {
        "mcpServers": {
            "cognizioware": {
                "type": "http",
                "url": url,
                "headers": headers,
            }
        }
    }

    target_dir = Path(run_dir) / "harness" / "mcp"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{role}.mcp.json"
    raw = json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8")
    target.write_bytes(raw)
    try:
        target.chmod(0o600)
    except (OSError, NotImplementedError):
        pass
    return target


def redact_mcp_config_for_display(data: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of an MCP config with Authorization redacted."""
    import copy

    result = copy.deepcopy(data)
    servers = result.get("mcpServers", {})
    if isinstance(servers, dict):
        for server in servers.values():
            if isinstance(server, dict) and isinstance(server.get("headers"), dict):
                headers = server["headers"]
                if "Authorization" in headers:
                    headers["Authorization"] = "***REDACTED***"
    return result
