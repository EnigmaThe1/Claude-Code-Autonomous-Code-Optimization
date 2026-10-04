# Copyright 2026 Bogdan Carp (@EnigmaThe1)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from execution import run_repository_command
from process_runner import run
from protocols import extract_result_json
from provider_config import (
    MIN_CLAUDE_VERSION,
    PROVIDER_PROFILES,
    RECOMMENDED_CLAUDE_VERSION,
    provider_env,
    safe_gateway_probe,
)
from repo_identity import SupervisorLease, find_repo_root, repo_state_dir
from repo_profile import BASE_PLUGINS, DEFERRED_PLUGINS, profile_repo
from repo_runtime import activate
from runtime_paths import data_home, package_root
from settings_policy import _inside_container
from state_store import load_json
from telemetry import redact_text
from toolchain_preflight import probe_toolchain

def user_layer_action(action: str) -> int:
    script = package_root() / "lib" / "user_layer.py"
    cp = subprocess.run([sys.executable, str(script), action, "--package-root", str(package_root())] if action == "install" else [sys.executable, str(script), action], text=True)
    return cp.returncode


def user_layer_status() -> dict[str, Any]:
    script = package_root() / "lib" / "user_layer.py"
    cp = run([sys.executable, str(script), "status"], timeout=15)
    if cp.returncode != 0:
        return {"installed": False, "error": (cp.stderr or cp.stdout).strip()}
    try:
        return json.loads(cp.stdout)
    except Exception:
        return {"installed": False, "error": "unparseable user-layer status"}


def _settings_audit(root: Path | None) -> dict[str, Any]:
    ch = Path(os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude"))).expanduser()
    paths = [ch / "settings.json"]
    # Managed/enterprise policy has higher precedence than user/project settings.
    # Include the standard Linux/macOS locations when present so doctor can expose
    # ask/deny/Auto policy that the worker cannot override.
    if sys.platform.startswith("linux"):
        paths.append(Path("/etc/claude-code/managed-settings.json"))
    elif sys.platform == "darwin":
        paths.append(Path("/Library/Application Support/ClaudeCode/managed-settings.json"))
    if root is not None:
        paths += [root / ".claude" / "settings.json", root / ".claude" / "settings.local.json"]
    findings: dict[str, Any] = {
        "files": [],
        "ask_rules": [],
        "deny_rules": [],
        "disable_auto_mode": [],
        "hooks": [],
        "mcp_configured": [],
        "errors": [],
    }
    for path in paths:
        if not path.exists():
            continue
        try:
            obj = json.loads(path.read_text())
        except Exception as exc:
            findings["errors"].append({"path": str(path), "error": str(exc)})
            continue
        if not isinstance(obj, dict):
            findings["errors"].append({"path": str(path), "error": "settings root is not an object"})
            continue
        findings["files"].append(str(path))
        perms = obj.get("permissions") if isinstance(obj.get("permissions"), dict) else {}
        for rule in perms.get("ask", []) if isinstance(perms.get("ask"), list) else []:
            findings["ask_rules"].append({"path": str(path), "rule": rule})
        for rule in perms.get("deny", []) if isinstance(perms.get("deny"), list) else []:
            findings["deny_rules"].append({"path": str(path), "rule": rule})
        if perms.get("disableAutoMode") == "disable":
            findings["disable_auto_mode"].append(str(path))
        hooks = obj.get("hooks")
        if isinstance(hooks, dict) and hooks:
            findings["hooks"].append({"path": str(path), "events": sorted(hooks)})
        if isinstance(obj.get("mcpServers"), dict) and obj.get("mcpServers"):
            findings["mcp_configured"].append({"path": str(path), "servers": sorted(obj["mcpServers"])})
    # ~/.claude.json can hold MCP configuration too. Read only keys, never values.
    global_config = ch.parent / ".claude.json" if ch.name == ".claude" else ch / "../.claude.json"
    try:
        global_config = global_config.resolve()
        if global_config.exists():
            obj = json.loads(global_config.read_text())
            if isinstance(obj, dict) and isinstance(obj.get("mcpServers"), dict) and obj.get("mcpServers"):
                findings["mcp_configured"].append({"path": str(global_config), "servers": sorted(obj["mcpServers"])})
    except Exception:
        pass
    return findings


def doctor(args: argparse.Namespace | None = None) -> int:
    checks: list[tuple[str, bool, str]] = []
    warnings: list[str] = []
    checks.append(("Python >= 3.10", sys.version_info >= (3, 10), sys.version.split()[0]))
    injected_git = sorted(
        key for key in os.environ
        if key in {"GIT_CONFIG_COUNT", "GIT_CONFIG_PARAMETERS"}
        or key.startswith("GIT_CONFIG_KEY_")
        or key.startswith("GIT_CONFIG_VALUE_")
    )
    checks.append((
        "Inherited Git config sanitiser",
        True,
        (
            "clean environment"
            if not injected_git
            else "will strip process-scoped variables: " + ", ".join(injected_git[:12])
        ),
    ))
    git = shutil.which("git")
    checks.append(("git", bool(git), git or "not found"))
    claude = shutil.which("claude")
    checks.append(("Claude Code", bool(claude), claude or "not found"))
    if claude:
        v = run(["claude", "--version"], timeout=15)
        vtext = (v.stdout or v.stderr).strip()[:200]
        m = re.search(r"(\d+)\.(\d+)\.(\d+)", vtext)
        parsed_version = tuple(map(int, m.groups())) if m else None
        version_ok = bool(parsed_version and parsed_version >= MIN_CLAUDE_VERSION)
        checks.append((f"Claude version >= {'.'.join(map(str, MIN_CLAUDE_VERSION))}", v.returncode == 0 and version_ok, vtext))
        if parsed_version and parsed_version < RECOMMENDED_CLAUDE_VERSION:
            warnings.append(
                f"Claude Code {vtext} meets the minimum but v{'.'.join(map(str, RECOMMENDED_CLAUDE_VERSION))}+ is recommended."
            )
        a = run(["claude", "auth", "status", "--text"], timeout=30)
        if a.returncode != 0:
            a = run(["claude", "auth", "status"], timeout=30)
        checks.append(("Claude authentication", a.returncode == 0, (a.stdout or a.stderr).strip()[:300]))
        help_cp = run(["claude", "--help"], timeout=15)
        help_text = (help_cp.stdout or help_cp.stderr or "").lower()
        checks.append(("Claude CLI exposes Auto permission mode", help_cp.returncode == 0 and "permission-mode" in help_text and "auto" in help_text, "model-specific Auto eligibility is proven by `claude-auto models qualify --level full`"))
        native_doctor = run(["claude", "doctor"], timeout=30)
        if native_doctor.returncode != 0:
            warnings.append("`claude doctor` reported configuration warnings/errors: " + (native_doctor.stderr or native_doctor.stdout)[-500:])

    ul = user_layer_status()
    checks.append(("Global user optimisation layer", bool(ul.get("installed") and ul.get("hook_present") and ul.get("owned_files_present")), json.dumps(ul, separators=(",", ":"))[:400]))
    writable = data_home()
    try:
        writable.mkdir(parents=True, exist_ok=True)
        test = writable / ".write-test"
        test.write_text("ok")
        test.unlink()
        ok = True
        detail = str(writable)
    except Exception as e:
        ok, detail = False, str(e)
    checks.append(("Autonomy data directory", ok, detail))

    if sys.platform.startswith("linux"):
        has_bwrap = bool(shutil.which("bwrap"))
        has_srt = bool(shutil.which("srt"))
        boundary_ok = False
        boundary_detail = "missing: install @anthropic-ai/sandbox-runtime or bubblewrap"
        if has_srt or has_bwrap:
            try:
                with tempfile.TemporaryDirectory(prefix="claude-auto-doctor-sandbox-") as td:
                    probe = run_repository_command(
                        Path(td),
                        ["/bin/true"],
                        timeout=15,
                        trust_repo_scripts=False,
                        unrestricted_host=False,
                        read_only_root=True,
                        read_allowlist_only=True,
                        max_output_bytes=32_768,
                    )
                boundary_ok = (
                    int(probe.get("returncode", 1)) == 0
                    and bool(probe.get("sandboxed", False))
                )
                boundary_detail = (
                    str(probe.get("execution_boundary") or "unknown")
                    if boundary_ok
                    else (
                        str(probe.get("stderr") or probe.get("stdout") or "sandbox probe failed")
                        .strip()
                        .replace("\n", " ")[:500]
                    )
                )
            except Exception as exc:
                boundary_detail = f"sandbox probe failed: {exc}"
        checks.append((
            "Supervisor repository-code sandbox",
            boundary_ok,
            boundary_detail,
        ))
        if not has_bwrap:
            warnings.append("bubblewrap/bwrap not found: Claude's native Linux sandbox and Claude Auto's bubblewrap supervisor fallback may be unavailable; an installed srt wrapper also requires its Linux sandbox prerequisites.")
        if not shutil.which("socat"):
            warnings.append("socat not found: Claude's Linux sandbox networking may be unavailable.")
        if _inside_container():
            warnings.append("Container detected: weaker nested-sandbox /proc mode is enabled only by explicit CLAUDE_AUTO_ALLOW_WEAKER_NESTED_SANDBOX=1 attestation.")

    root: Path | None = None
    raw_repo = getattr(args, "repo", None) if args is not None else None
    if raw_repo:
        root = find_repo_root(raw_repo)
    elif args is not None:
        try:
            root = find_repo_root(None)
        except SystemExit:
            root = None
    if root is not None:
        try:
            prof = profile_repo(root)
            for row in probe_toolchain(root, {"languages": prof.languages}):
                checks.append((
                    f"Repository toolchain: {row['tool']}",
                    bool(row.get("ok")),
                    f"{row.get('detail')} ({row.get('reason')})",
                ))
        except Exception as exc:
            warnings.append(f"Repository toolchain preflight could not complete: {exc}")

    audit = _settings_audit(root)
    if audit["errors"]:
        checks.append(("Claude settings JSON", False, json.dumps(audit["errors"], separators=(",", ":"))[:500]))
    else:
        checks.append(("Claude settings JSON", True, f"{len(audit['files'])} readable settings file(s)"))
    if audit["disable_auto_mode"]:
        checks.append(("Auto mode available by settings", False, "disableAutoMode=disable in " + ", ".join(audit["disable_auto_mode"])))
    else:
        checks.append(("Auto mode available by settings", True, "no file-based disableAutoMode blocker found"))
    if audit["ask_rules"]:
        warnings.append(
            "Headless blocker risk: explicit permissions.ask rules still prompt even in Auto. `claude-auto run` cannot answer them: "
            + json.dumps(audit["ask_rules"], separators=(",", ":"))[:800]
        )
    if audit["mcp_configured"]:
        warnings.append(
            "MCP servers are configured. Tools marked requiresUserInteraction or organization-configured as ask cannot be auto-approved in headless mode; PermissionDenied telemetry will identify any actual block: "
            + json.dumps(audit["mcp_configured"], separators=(",", ":"))[:800]
        )
    if audit["hooks"]:
        warnings.append("Existing hooks affect interactive or explicitly compatibility-mode sessions. Headless Balanced/Isolated Full/Unattended default hermetic, but review hook events before opting back into compatibility: " + json.dumps(audit["hooks"], separators=(",", ":"))[:800])

    width = max(len(x[0]) for x in checks)
    all_ok = True
    for name, ok, detail in checks:
        all_ok &= ok
        print(f"{'PASS' if ok else 'FAIL':4}  {name:<{width}}  {detail}")
    for warning in warnings:
        print(f"WARN  {warning}")
    return 0 if all_ok else 1


def plugin_recommendations(root: Path | None) -> tuple[Path | None, list[str], list[str]]:
    if root is None:
        return None, list(BASE_PLUGINS), []
    sd = repo_state_dir(root)
    with SupervisorLease(sd, root):
        activate(root)
        prof = load_json(sd / "profile.json", {})
    return sd, prof.get("recommended_plugins", BASE_PLUGINS), prof.get("lsp_candidates", [])


def install_plugins(root: Path | None, install: bool, include_lsp: bool = False, include_deferred: bool = False) -> int:
    sd, plugins, lsp_candidates = plugin_recommendations(root)
    print(f"Repository: {root}" if root else "Scope: user-level core plugins for all repositories")
    print("Core recommended plugins:")
    for p in plugins:
        print(f"  - {p}")
    if DEFERRED_PLUGINS:
        print("Deferred plugin candidates (known upstream packaging/discovery defects; not auto-installed):")
        for p in DEFERRED_PLUGINS:
            print(f"  - {p}")
    if lsp_candidates:
        print("LSP compatibility candidates (not auto-installed):")
        for p in lsp_candidates:
            print(f"  - {p}")
        print("  Note: official LSP marketplace registration has an open upstream issue; use --include-lsp only after local verification.")
    if not install:
        print("\nNo plugins changed. Add --install to install stable core plugins at user scope.")
        return 0
    if include_deferred:
        plugins = list(dict.fromkeys([*plugins, *DEFERRED_PLUGINS]))
    if include_lsp:
        plugins = list(dict.fromkeys([*plugins, *lsp_candidates]))
    if not shutil.which("claude"):
        print("Claude Code is not installed; cannot install plugins.", file=sys.stderr)
        return 1

    # Ensure official marketplace is registered. Safe if already present; failure is reported but not fatal.
    cp = run(["claude", "plugin", "marketplace", "add", "anthropics/claude-plugins-official", "--scope", "user"], timeout=120)
    if cp.returncode != 0 and "already" not in (cp.stdout + cp.stderr).lower():
        print("Warning: could not add/confirm official marketplace:")
        print((cp.stderr or cp.stdout).strip())

    failures = []
    for p in plugins:
        print(f"Installing {p} ...")
        cp = run(["claude", "plugin", "install", f"{p}@claude-plugins-official", "--scope", "user"], timeout=300)
        if cp.returncode == 0:
            print((cp.stdout or "installed").strip())
        else:
            failures.append(p)
            print((cp.stderr or cp.stdout or "installation failed").strip())
    if failures:
        print("\nSome plugins were not installed: " + ", ".join(failures))
        print("The harness remains usable; plugin installation is an optimisation, not a runtime dependency.")
        return 2
    return 0




def gateway_list() -> int:
    rows = []
    for name, spec in PROVIDER_PROFILES.items():
        rows.append({
            "name": name,
            "base_url": spec.get("base_url"),
            "token_env": spec.get("token_env"),
            "model_discovery": spec.get("model_discovery", False),
            "description": spec.get("description"),
        })
    print(json.dumps(rows, indent=2))
    return 0


def gateway_doctor(args: argparse.Namespace) -> int:
    provider = args.provider
    if provider == "native":
        print("native: Claude Code current authentication/provider path; no gateway override is applied.")
        cp = run(["claude", "auth", "status", "--text"], timeout=30)
        if cp.returncode != 0:
            cp = run(["claude", "auth", "status"], timeout=30)
        print((cp.stdout or cp.stderr).strip())
        return 0 if cp.returncode == 0 else 1

    env, detail = provider_env(
        provider,
        gateway_url=args.gateway_url,
        gateway_token_env=args.gateway_token_env,
        enable_discovery=args.gateway_discovery,
        isolate_provider_profile=args.isolate_provider_profile,
        gateway_hints=args.gateway_hints,
    )
    local_ok = True
    if provider == "ccr":
        ccr = shutil.which("ccr")
        print(f"CCR CLI: {'PASS ' + ccr if ccr else 'WARN not installed (remote/manual CCR endpoint may still be usable)'}")
    elif provider == "litellm":
        exe = shutil.which("litellm")
        print(f"LiteLLM CLI: {'PASS ' + exe if exe else 'INFO no local CLI (remote LiteLLM endpoint may still be usable)'}")

    base = detail.get("base_url")
    print(f"Endpoint: {base}")
    print(f"Credential source: ${detail.get('token_env')}" if detail.get("token_env") else "Credential source: none/custom")
    print(f"Gateway hint headers: {'enabled' if detail.get('gateway_hints') else 'disabled'}")
    print(f"Model discovery: {'enabled' if detail.get('model_discovery') else 'disabled'}")

    discovery_ok = None
    if args.no_network:
        print("Network probes: SKIPPED")
    else:
        token = env.get("ANTHROPIC_AUTH_TOKEN") or None
        discovery_ok, message = safe_gateway_probe(str(base), token)
        print(f"/v1/models discovery probe: {'PASS' if discovery_ok else 'INFO/UNSUPPORTED'} {message}")
        if not discovery_ok:
            print("  Discovery failure is not treated as a Messages API failure; some compatible gateways do not expose /v1/models.")

    if args.probe_model:
        # Actual harness compatibility probe. No tools, no MCP, no project/user
        # customizations beyond the explicit settings path, and one turn only.
        probe_cmd = [
            "claude", "--restricted",
            "--setting-sources", "",
            "--tools", "",
            "--disallowed-tools", "*",
            "--model", args.probe_model,
            "-p", "--output-format", "json", "--max-turns", "1",
            "--permission-prompts", "none",
            "--no-session-persistence",
            "--exclude-dynamic-system-prompt-sections",
            "Reply with exactly GATEWAY_OK and nothing else.",
        ]
        cp = run(probe_cmd, cwd=Path.cwd(), timeout=args.timeout, env=env)
        text_result, _, _ = extract_result_json(cp.stdout)
        probe_ok = cp.returncode == 0 and text_result.strip() == "GATEWAY_OK"
        print(f"Messages/harness probe ({args.probe_model}): {'PASS' if probe_ok else 'FAIL'}")
        if not probe_ok:
            detail_text = redact_text(cp.stderr or cp.stdout or "no response", env, 1000)
            if detail_text:
                print(detail_text)
        return 0 if local_ok and probe_ok else 1

    print("Messages/harness probe: NOT RUN (supply --probe-model MODEL to verify actual Claude Code request compatibility).")
    return 0 if local_ok else 1

def gateway_install(args: argparse.Namespace) -> int:
    provider = args.provider
    if provider in {"native", "openrouter", "custom"}:
        print(f"{provider} requires no local gateway package installation.")
        return 0
    if provider == "ccr":
        node = shutil.which("node")
        npm = shutil.which("npm")
        if not node or not npm:
            print("CCR requires Node.js 22+ and npm.", file=sys.stderr)
            return 1
        v = run(["node", "--version"])
        m = re.search(r"v?(\d+)", v.stdout or v.stderr)
        if not m or int(m.group(1)) < 22:
            print(f"CCR requires Node.js 22+; found {(v.stdout or v.stderr).strip()}", file=sys.stderr)
            return 1
        if not args.yes:
            print("Would run: npm install -g @musistudio/claude-code-router@latest")
            print("Re-run with --yes to install. No repository files will be modified.")
            return 0
        cp = run([npm, "install", "-g", "@musistudio/claude-code-router@latest"], timeout=900)
        print((cp.stdout or cp.stderr).strip())
        return cp.returncode
    if provider == "litellm":
        uv = shutil.which("uv")
        if not uv:
            print("LiteLLM self-install uses uv. Install uv first or install LiteLLM manually.", file=sys.stderr)
            return 1
        cmd = [uv, "tool", "upgrade", "litellm"] if shutil.which("litellm") else [uv, "tool", "install", "litellm[proxy]"]
        if not args.yes:
            print("Would run: " + " ".join(cmd))
            print("Re-run with --yes to install. No repository files will be modified.")
            return 0
        cp = run(cmd, timeout=900)
        print((cp.stdout or cp.stderr).strip())
        return cp.returncode
    return 2


def gateway_service(args: argparse.Namespace) -> int:
    if args.provider != "ccr":
        print("Managed start/stop/UI is currently implemented only for CCR; OpenRouter is hosted and LiteLLM needs an operator config file.", file=sys.stderr)
        return 2
    ccr = shutil.which("ccr")
    if not ccr:
        print("CCR is not installed. Use `claude-auto gateway install ccr --yes` first.", file=sys.stderr)
        return 1
    if args.action == "start":
        cp = run([ccr, "start", "--no-open"], timeout=120)
    elif args.action == "stop":
        cp = run([ccr, "stop"], timeout=120)
    else:
        # UI intentionally remains interactive/background; it is how CCR safely collects provider credentials.
        return subprocess.call([ccr, "ui"])
    print((cp.stdout or cp.stderr).strip())
    return cp.returncode
