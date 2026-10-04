from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

from environment_policy import sanitised_subprocess_env
from process_runner import run
from runtime_paths import data_home, ensure_private_dir, model_registry_path, utcnow
from state_store import json_dump, load_json, sha256_text


MIN_CLAUDE_VERSION = (2, 1, 273)
RECOMMENDED_CLAUDE_VERSION = (2, 1, 283)


PROVIDER_PROFILES = {
    "native": {
        "description": "Use the current Claude Code authentication/provider path without overriding gateway environment variables.",
        "base_url": None,
        "token_env": None,
        "model_discovery": False,
    },
    "openrouter": {
        "description": "Direct hosted Anthropic-compatible gateway; simplest multi-provider option, with non-Anthropic compatibility requiring qualification.",
        "base_url": "https://openrouter.ai/api",
        "token_env": "OPENROUTER_API_KEY",
        "model_discovery": True,
    },
    "ccr": {
        "description": "Claude Code Router local control plane for per-role routing, fallbacks, observability, and local/hosted providers.",
        "base_url": "http://127.0.0.1:3456",
        "token_env": "CCR_CLIENT_KEY",
        "model_discovery": True,
    },
    "litellm": {
        "description": "LiteLLM self-hosted gateway for provider-neutral routing, budgets, load balancing, fallbacks, and model discovery.",
        "base_url": "http://127.0.0.1:4000",
        "token_env": "LITELLM_MASTER_KEY",
        "model_discovery": True,
    },
    "custom": {
        "description": "Any operator-supplied Anthropic Messages-compatible gateway.",
        "base_url": None,
        "token_env": "CLAUDE_AUTO_GATEWAY_TOKEN",
        "model_discovery": True,
    },
}


def claude_version_tuple() -> tuple[tuple[int, ...] | None, str]:
    cp = run(["claude", "--version"])
    raw = (cp.stdout or cp.stderr or "").strip()
    if cp.returncode != 0:
        return None, raw or "claude command unavailable"
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", raw)
    return (tuple(int(x) for x in m.groups()) if m else None), raw


def require_supported_claude() -> str:
    ver, raw = claude_version_tuple()
    if ver is None:
        raise SystemExit(f"Could not determine Claude Code version: {raw}")
    if ver < MIN_CLAUDE_VERSION:
        raise SystemExit(f"Claude Code {raw} is older than the Claude Auto minimum {'.'.join(map(str,MIN_CLAUDE_VERSION))}. Upgrade Claude Code before autonomous execution.")
    if ver < RECOMMENDED_CLAUDE_VERSION:
        print(f"Notice: Claude Code {raw} meets the minimum but Claude Auto recommends {'.'.join(map(str,RECOMMENDED_CLAUDE_VERSION))} or newer.", file=sys.stderr)
    return raw


def provider_env(
    provider: str,
    *,
    gateway_url: str | None = None,
    gateway_token_env: str | None = None,
    enable_discovery: bool | None = None,
    isolate_provider_profile: bool = False,
    gateway_hints: bool = True,
    opus_model: str | None = None,
    sonnet_model: str | None = None,
    haiku_model: str | None = None,
    subagent_model: str | None = None,
) -> tuple[dict[str, str], dict[str, Any]]:
    if provider not in PROVIDER_PROFILES:
        raise SystemExit(f"Unknown provider profile: {provider}")
    spec = PROVIDER_PROFILES[provider]
    env = sanitised_subprocess_env()
    detail: dict[str, Any] = {
        "provider": provider,
        "description": spec["description"],
        "base_url": None,
        "token_env": None,
        "model_discovery": False,
        "gateway_hints": False,
        "isolated_profile": False,
    }

    if provider != "native":
        base_url = gateway_url or env.get(f"CLAUDE_AUTO_{provider.upper()}_URL") or spec.get("base_url")
        if not base_url:
            raise SystemExit(f"Provider {provider!r} requires --gateway-url or CLAUDE_AUTO_{provider.upper()}_URL")
        token_env = gateway_token_env or spec.get("token_env")
        token = env.get(token_env or "") if token_env else None
        if provider == "litellm" and not token and env.get("LITELLM_API_KEY"):
            token_env, token = "LITELLM_API_KEY", env.get("LITELLM_API_KEY")
        if provider != "custom" and not token:
            raise SystemExit(
                f"Provider {provider!r} needs a gateway credential in ${token_env}. "
                "The optimisation pack never stores provider secrets."
            )
        env["ANTHROPIC_BASE_URL"] = str(base_url).rstrip("/")
        env["ANTHROPIC_AUTH_TOKEN"] = token or ""
        # Never forward a caller's native Anthropic API key to a third-party gateway.
        # Keep the variable explicitly empty rather than merely unset: some Claude Code
        # gateway paths may otherwise fall back to cached/native Anthropic auth.
        env["ANTHROPIC_API_KEY"] = ""
        # Custom/gateway connections can gain better body-cache reuse when attribution
        # headers are omitted; this does not affect direct native Anthropic sessions.
        env.setdefault("CLAUDE_CODE_ATTRIBUTION_HEADER", "0")

        if gateway_hints:
            env["CLAUDE_CODE_GATEWAY_HINT_HEADERS"] = "1"
        else:
            env.pop("CLAUDE_CODE_GATEWAY_HINT_HEADERS", None)

        if isolate_provider_profile:
            gateway_identity = hashlib.sha256(
                f"{provider}|{env['ANTHROPIC_BASE_URL']}".encode("utf-8")
            ).hexdigest()[:12]
            isolated_config = ensure_private_dir(data_home() / "provider-configs" / f"{provider}-{gateway_identity}")
            env["CLAUDE_CONFIG_DIR"] = str(isolated_config)
            detail["claude_config_dir"] = str(isolated_config)
            detail["isolated_profile"] = True

        discovery = spec.get("model_discovery", False) if enable_discovery is None else enable_discovery
        if discovery:
            env["CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY"] = "1"
        else:
            env.pop("CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY", None)
        detail.update({
            "base_url": env["ANTHROPIC_BASE_URL"],
            "token_env": token_env,
            "model_discovery": bool(discovery),
            "gateway_hints": bool(gateway_hints),
        })

    role_vars = {
        "ANTHROPIC_DEFAULT_OPUS_MODEL": opus_model,
        "ANTHROPIC_DEFAULT_SONNET_MODEL": sonnet_model,
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": haiku_model,
        "CLAUDE_CODE_SUBAGENT_MODEL": subagent_model,
    }
    for key, value in role_vars.items():
        if value:
            env[key] = value
    detail["role_models"] = {k: v for k, v in role_vars.items() if v}
    return env, detail


def provider_from_args(args: argparse.Namespace, prefix: str = "") -> tuple[dict[str, str], dict[str, Any]]:
    def get(name: str, default: Any = None) -> Any:
        return getattr(args, f"{prefix}{name}", default)
    provider = get("provider", None)
    if not provider:
        provider = getattr(args, "provider", "native") if prefix else "native"
    return provider_env(
        provider,
        gateway_url=get("gateway_url"),
        gateway_token_env=get("gateway_token_env"),
        enable_discovery=get("gateway_discovery"),
        isolate_provider_profile=bool(get("isolate_provider_profile", False)),
        gateway_hints=bool(get("gateway_hints", True)),
        opus_model=get("opus_model"),
        sonnet_model=get("sonnet_model"),
        haiku_model=get("haiku_model"),
        subagent_model=get("subagent_model"),
    )


def safe_gateway_probe(base_url: str, token: str | None, timeout: int = 8) -> tuple[bool, str]:
    """Optional discovery endpoint probe; not proof of Messages API compatibility."""
    url = base_url.rstrip("/") + "/v1/models"
    headers = {"Accept": "application/json"}
    if token:
        # Match ANTHROPIC_AUTH_TOKEN semantics: bearer Authorization only.
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(2048).decode("utf-8", errors="replace")
            return 200 <= resp.status < 300, f"HTTP {resp.status}; {body[:300]}"
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}: {e.reason}"
    except Exception as e:
        return False, str(e)

def load_model_registry() -> dict[str, Any]:
    return load_json(model_registry_path(), {"schema_version": 1, "models": {}})


def _model_registry_name(model: str | None) -> str:
    return model or "__native_default__"

def save_model_qualification(provider: str, model: str | None, result: dict[str, Any]) -> None:
    reg = load_model_registry()
    reg.setdefault("models", {})[f"{provider}:{_model_registry_name(model)}"] = result
    reg["updated_at"] = utcnow()
    json_dump(model_registry_path(), reg)


def qualification_record(provider: str, model: str | None) -> dict[str, Any] | None:
    return load_model_registry().get("models", {}).get(f"{provider}:{_model_registry_name(model)}")

def _claude_version_text() -> str:
    cp = run(["claude", "--version"])
    return (cp.stdout or cp.stderr or "unknown").strip()[:200]

def qualification_route_fingerprint(provider: str, model: str | None, values: dict[str, Any]) -> str:
    obj={"provider":provider,"model":_model_registry_name(model),"gateway_url":values.get("gateway_url"),"gateway_token_env":values.get("gateway_token_env"),"gateway_discovery":values.get("gateway_discovery"),"isolate_provider_profile":values.get("isolate_provider_profile"),"gateway_hints":values.get("gateway_hints"),"claude_code_version":_claude_version_text(),"qualification_schema":2}
    return sha256_text(json.dumps(obj, sort_keys=True, separators=(",", ":")))

def qualification_is_fresh(rec: dict[str, Any] | None, expected_fp: str, ttl_days: int = 7) -> bool:
    if not rec or rec.get("route_fingerprint") != expected_fp: return False
    try:
        q=datetime.fromisoformat(str(rec.get("qualified_at"))); age=(datetime.now(timezone.utc)-q).total_seconds()
        return age <= ttl_days*86400
    except Exception:
        return False


QUALIFICATION_RANK = {"FAILED": 0, "HARNESS_SMOKE_READONLY": 1, "AUTO_EXECUTION": 2, "AUTONOMOUS_FULL": 3}

def warn_model_qualification(provider: str, model: str | None, role: str, required_level: str | None = None) -> None:
    if not model:
        return
    required = required_level or ("AUTONOMOUS_FULL" if role == "main" else "HARNESS_SMOKE_READONLY")
    rec = qualification_record(provider, model)
    if not rec:
        print(f"Notice: {role} model {provider}:{model} has no recorded {required} qualification.")
        return
    level = str(rec.get("qualification_level") or "FAILED")
    if not rec.get("compatible") or QUALIFICATION_RANK.get(level, 0) < QUALIFICATION_RANK.get(required, 0):
        print(f"WARNING: {role} model {provider}:{model} qualification is {level}; {required} is required for this role.", file=sys.stderr)


def _qualification_provider_values(args: argparse.Namespace, prefix: str = "") -> tuple[str, dict[str, Any]]:
    """Resolve the provider lane used by an autonomous model role.

    Same-provider fallback models inherit the main gateway configuration.  An
    explicitly different fallback/challenger provider uses its own prefixed
    configuration.  This keeps qualification evidence attached to the route that
    will actually execute work.
    """
    explicit_provider = getattr(args, f"{prefix}provider", None) if prefix else getattr(args, "provider", "native")
    inherit_main = bool(prefix and not explicit_provider and prefix != "challenger_")
    provider = (getattr(args, "provider", "native") if inherit_main else explicit_provider) or "native"

    def value(name: str, default: Any = None) -> Any:
        if inherit_main:
            return getattr(args, name, default)
        return getattr(args, f"{prefix}{name}", default)

    return provider, {
        "gateway_url": value("gateway_url"),
        "gateway_token_env": value("gateway_token_env"),
        "gateway_discovery": value("gateway_discovery"),
        "isolate_provider_profile": bool(value("isolate_provider_profile", False)),
        "gateway_hints": bool(value("gateway_hints", True)),
    }
