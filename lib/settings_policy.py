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

import json
import os
import shlex
import shutil
import sys
from pathlib import Path
from typing import Any

from runtime_paths import data_home, package_root
from state_store import load_json


AUTONOMY_PROFILES = {
    "balanced": {
        "description": "Default: workspace-aware sandbox, no broad outside-working-directory read prompt, classifier-reviewed unsandboxed retries, routine local/reversible engineering pre-authorised.",
        "allow_unsandboxed": True,
        "block_reads_outside_workdirs": False,
        "strict_secret_reads": False,
        "isolated_full": False,
        "unrestricted": False,
    },
    "strict": {
        "description": "Strict sandbox: broad outside-working-directory read block, no unsandboxed retry escape hatch and broader secret-file read denials.",
        "allow_unsandboxed": False,
        "block_reads_outside_workdirs": True,
        "strict_secret_reads": True,
        "isolated_full": False,
        "unrestricted": False,
    },
    "isolated-full": {
        "description": "Maximum autonomy for an explicitly attested disposable container/VM only; bypass permissions inside the outer isolation boundary.",
        "allow_unsandboxed": True,
        "block_reads_outside_workdirs": False,
        "strict_secret_reads": False,
        "isolated_full": True,
        "unrestricted": False,
    },
    "unattended": {
        "description": "Explicit user pre-authorisation for fully unattended execution on the current host. Claude Auto removes its own sandbox, path, container and secret-read restrictions and uses bypass permissions. External OS/provider/organisation controls still apply.",
        "allow_unsandboxed": True,
        "block_reads_outside_workdirs": False,
        "strict_secret_reads": False,
        "isolated_full": False,
        "unrestricted": True,
    },
}


def _inside_container() -> bool:
    return Path("/.dockerenv").exists() or Path("/run/.containerenv").exists() or os.environ.get("container") in {"docker", "podman"}


def _append_command_hook(settings: dict[str, Any], event: str, script: Path, matcher: str | None = None) -> None:
    entry: dict[str, Any] = {
        "hooks": [{
            "type": "command",
            "command": f"python3 -B {shlex.quote(str(script))}",
            "timeout": 5,
        }]
    }
    if matcher:
        entry["matcher"] = matcher
    settings.setdefault("hooks", {}).setdefault(event, []).append(entry)


def _compat_excluded_commands(repo_profile: dict[str, Any] | None) -> list[str]:
    """Return narrowly-scoped compatibility exclusions evidenced on this host.

    Claude Auto deliberately does NOT exclude Docker/Podman/nerdctl wholesale.  Claude
    Code documents excludedCommands as unsandboxed execution, so broad container
    runtime exclusions grant host-equivalent authority on many developer
    machines.  Container commands remain subject to the sandbox/Auto classifier
    and the token-aware Docker guard.
    """
    excluded: list[str] = []
    repo_profile = repo_profile or {}
    if sys.platform == "darwin":
        # Preserve only documented host-tool compatibility exceptions.
        for exe in ("gh", "gcloud"):
            if shutil.which(exe):
                excluded.append(f"{exe} *")
        langs = set(repo_profile.get("languages", []))
        if "terraform" in langs and shutil.which("terraform"):
            excluded.append("terraform *")
    return excluded


def _repo_network_domains(repo_profile: dict[str, Any] | None) -> list[str]:
    """Pre-allow only canonical package/toolchain hosts evidenced by the repo."""
    repo_profile = repo_profile or {}
    langs = set(repo_profile.get("languages") or [])
    manifests = {Path(x).name for x in (repo_profile.get("manifests") or []) if isinstance(x, str)}
    domains: list[str] = []
    if "rust" in langs or "Cargo.toml" in manifests:
        domains += [
            "static.rust-lang.org",
            "static.rustup.rs",
            "crates.io",
            "index.crates.io",
            "static.crates.io",
        ]
    if "python" in langs or {"pyproject.toml", "uv.lock"} & manifests:
        domains += ["pypi.org", "files.pythonhosted.org"]
    if {"javascript", "typescript", "javascript-typescript"} & langs or "package.json" in manifests:
        domains += ["registry.npmjs.org"]
    if "go" in langs or "go.mod" in manifests:
        domains += ["proxy.golang.org", "sum.golang.org"]
    if {"java", "java-kotlin", "kotlin"} & langs:
        domains += ["repo.maven.apache.org", "services.gradle.org", "plugins.gradle.org"]
    return list(dict.fromkeys(domains))


DOTENV_SAMPLE_NAMES = {".env.example", ".env.sample", ".env.template", ".env.dist"}


def _discovered_dotenv_paths(repo_root: Path, max_entries: int = 50_000) -> list[Path]:
    """Return present real-secret .env paths without following symlinked dirs."""
    root = repo_root.resolve()
    paths: list[Path] = []
    seen = 0
    prune = {
        ".git", ".hg", ".svn", "node_modules", ".venv", "venv", "env",
        "dist", "build", "target", ".tox", ".nox", ".gradle", ".idea",
        ".pytest_cache", "__pycache__",
    }
    try:
        walker = os.walk(root, topdown=True, followlinks=False)
        for base, dirs, files in walker:
            dirs[:] = [d for d in dirs if d not in prune]
            for name in files:
                seen += 1
                if seen > max_entries:
                    return paths
                if name in DOTENV_SAMPLE_NAMES:
                    continue
                if name != ".env" and not name.startswith(".env."):
                    continue
                p = (Path(base) / name).resolve(strict=False)
                try:
                    p.relative_to(root)
                except ValueError:
                    continue
                if p not in paths:
                    paths.append(p)
    except OSError:
        pass
    return paths


def _discovered_dotenv_read_denies(repo_root: Path, max_entries: int = 50_000) -> list[str]:
    """Return exact Claude Read denies for present real-secret .env files."""
    root = repo_root.resolve()
    rules: list[str] = []
    for p in _discovered_dotenv_paths(root, max_entries=max_entries):
        try:
            rel = p.relative_to(root).as_posix()
        except ValueError:
            continue
        rule = f"Read(./{rel})"
        if rule not in rules:
            rules.append(rule)
    return rules


def _literal_absolute_rule_path(raw: str) -> str | None:
    try:
        p = Path(raw).expanduser().resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return None
    text = p.as_posix()
    escaped = ""
    for ch in text:
        if ch in {"\\", "*", "?", "[", "]", "!"}:
            escaped += "\\" + ch
        else:
            escaped += ch
    return "//" + escaped.lstrip("/")


def _grant_constraints(
    grants: list[dict[str, Any]],
    capability: str,
) -> tuple[list[str], list[str]]:
    paths: list[str] = []
    hashes: list[str] = []
    for grant in grants:
        if not isinstance(grant, dict) or grant.get("capability") != capability:
            continue
        constraints = grant.get("constraints")
        if not isinstance(constraints, dict):
            continue
        for raw in constraints.get("file_paths", []) if isinstance(constraints.get("file_paths"), list) else []:
            if isinstance(raw, str) and raw.strip():
                paths.append(raw.strip())
        for raw in constraints.get("command_sha256", []) if isinstance(constraints.get("command_sha256"), list) else []:
            digest = str(raw).lower()
            if len(digest) == 64 and all(ch in "0123456789abcdef" for ch in digest):
                hashes.append(digest)
    return sorted(set(paths)), sorted(set(hashes))


def make_settings(
    state_dir: Path,
    memory_mode: str = "external",
    autonomy_profile: str = "balanced",
    repo_profile: dict[str, Any] | None = None,
    permission_overrides: set[str] | None = None,
    permission_grants: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if autonomy_profile not in AUTONOMY_PROFILES:
        raise SystemExit(f"Unknown autonomy profile: {autonomy_profile}")
    profile = AUTONOMY_PROFILES[autonomy_profile]
    overrides = set(permission_overrides or ())
    grants = [g for g in (permission_grants or []) if isinstance(g, dict)]
    unrestricted = bool(profile.get("unrestricted")) or "unrestricted" in overrides
    outside_paths, outside_hashes = _grant_constraints(grants, "outside-repository")
    secret_paths, _secret_hashes = _grant_constraints(grants, "secret-read")
    _container_paths, container_hashes = _grant_constraints(grants, "container-host-authority")
    outside_relaxed = unrestricted
    read_outside_relaxed = unrestricted or bool(outside_paths or secret_paths)
    container_relaxed = unrestricted
    secret_relaxed = unrestricted
    template = load_json(package_root() / "templates" / "settings.json", {})
    template["autoMemoryEnabled"] = (memory_mode == "hybrid")
    template.setdefault("env", {})
    template["env"].update({
        "CLAUDE_AUTONOMY_STATE_DIR": str(state_dir),
        "CLAUDE_AUTONOMY_ENABLED": "1",
        "CLAUDE_AUTONOMY_PROFILE": autonomy_profile,
        "CLAUDE_AUTO_PACKAGE_ROOT": str(package_root().resolve()),
        "PYTHONDONTWRITEBYTECODE": "1",
    })

    sandbox = template.setdefault("sandbox", {})
    if profile["isolated_full"] or unrestricted:
        # isolated-full trusts an attested outer boundary. unattended is different:
        # the user explicitly pre-authorises host-level autonomous execution.
        sandbox["enabled"] = False
        sandbox["allowUnsandboxedCommands"] = True
        sandbox["failIfUnavailable"] = False
    else:
        sandbox["enabled"] = True
        explicit_relaxation = bool(outside_hashes or container_hashes)
        sandbox["allowUnsandboxedCommands"] = bool(profile["allow_unsandboxed"]) or explicit_relaxation
        # Strict normally fails closed. A user-approved scoped exception may use
        # Claude Code's unsandboxed fallback for that supervisor cycle.
        sandbox["failIfUnavailable"] = autonomy_profile == "strict" and not explicit_relaxation
        # Weak nested sandboxing is never inferred merely from /.dockerenv. It
        # materially weakens isolation and therefore requires an explicit operator
        # assertion that an outer disposable container/VM is the real boundary.
        sandbox["enableWeakerNestedSandbox"] = bool(
            _inside_container() and os.environ.get("CLAUDE_AUTO_ALLOW_WEAKER_NESTED_SANDBOX") == "1"
        )

    permissions = template.setdefault("permissions", {})
    if secret_relaxed:
        permissions["deny"] = []
    deny = permissions.setdefault("deny", [])
    allow = permissions.setdefault("allow", [])

    if autonomy_profile == "balanced":
        # Recovery/promotion is delegated only to package-owned fail-closed
        # helpers. Do not broaden this into raw rm or git merge authority.
        for recovery_rule in (
            "Bash(claude-auto cleanup-untracked *)",
            "Bash(claude-auto promote-ff *)",
        ):
            if recovery_rule not in allow:
                allow.append(recovery_rule)

    # Resource-scoped approvals add only exact file rules. Deterministic
    # hooks below enforce exact paths even when Claude Code needs a parent
    # additionalDirectory to make the file reachable.
    for raw in outside_paths:
        pattern = _literal_absolute_rule_path(raw)
        if not pattern:
            continue
        for rule in (
            f"Read({pattern})",
            f"Edit({pattern})",
            f"Write({pattern})",
            f"NotebookEdit({pattern})",
        ):
            if rule not in allow:
                allow.append(rule)
    for raw in secret_paths:
        pattern = _literal_absolute_rule_path(raw)
        if pattern:
            rule = f"Read({pattern})"
            if rule not in allow:
                allow.append(rule)

    # Claude Auto: make the workspace boundary explicit and remove the broad read gate
    # from Balanced autonomous sessions. Strict/read-only sessions preserve it.
    permissions["blockReadsOutsideWorkingDirectories"] = False if read_outside_relaxed else bool(profile["block_reads_outside_workdirs"])
    repo_root: str | None = None
    if repo_profile and repo_profile.get("repo_root"):
        repo_root = str(Path(repo_profile["repo_root"]).expanduser().resolve())
        template.setdefault("env", {})["CLAUDE_AUTO_REPO_ROOT"] = repo_root
        if outside_paths:
            template["env"]["CLAUDE_AUTO_APPROVED_OUTSIDE_PATHS"] = json.dumps(outside_paths)
        if secret_paths:
            template["env"]["CLAUDE_AUTO_APPROVED_SECRET_PATHS"] = json.dumps(secret_paths)
        if outside_hashes:
            template["env"]["CLAUDE_AUTO_APPROVED_OUTSIDE_COMMAND_HASHES"] = json.dumps(outside_hashes)
        if container_hashes:
            template["env"]["CLAUDE_AUTO_APPROVED_CONTAINER_COMMAND_HASHES"] = json.dumps(container_hashes)
        additional = permissions.setdefault("additionalDirectories", [])
        if repo_root not in additional:
            additional.append(repo_root)

        # Repository semantic governance is independent of the runtime autonomy
        # profile. In particular, Unattended may widen host/runtime authority but
        # must never make planning/control authority writable by the product worker.
        snapshot = load_json(state_dir / "governance" / "snapshot.json", {})
        protected_rel: list[str] = []
        if isinstance(snapshot, dict):
            raw_paths = snapshot.get("protected_paths")
            if isinstance(raw_paths, list):
                protected_rel.extend(
                    str(raw) for raw in raw_paths
                    if isinstance(raw, str) and raw.strip()
                )

        # RC3 compatibility fallback: settings generated before the first RC4
        # activation still protect the legacy one-file canonical plan.
        if not protected_rel:
            repair_policy = load_json(state_dir / "planning-repair" / "policy.json", {})
            canonical_plan = repair_policy.get("canonical_plan") if isinstance(repair_policy, dict) else None
            if isinstance(canonical_plan, str) and canonical_plan.strip():
                protected_rel.append(canonical_plan)

        protected_abs: list[str] = []
        for raw in sorted(set(protected_rel)):
            try:
                rel_path = Path(raw)
                if rel_path.is_absolute() or ".." in rel_path.parts or rel_path == Path("."):
                    continue
                protected = (Path(repo_root) / rel_path).resolve(strict=False)
                protected.relative_to(Path(repo_root))
            except (OSError, RuntimeError, ValueError):
                continue
            protected_abs.append(str(protected))
            rel = rel_path.as_posix()
            for rule in (
                f"Edit(./{rel})",
                f"Write(./{rel})",
                f"NotebookEdit(./{rel})",
            ):
                if rule not in deny:
                    deny.append(rule)
        if protected_abs:
            template["env"]["CLAUDE_AUTO_PROTECTED_REPO_PATHS"] = json.dumps(protected_abs)
        if profile["isolated_full"] or unrestricted:
            # Preserve semantic planning/task/control authority even when the
            # operator deliberately grants broad host/runtime write authority.
            template["env"]["CLAUDE_AUTO_SEMANTIC_ONLY_WRITE_GUARD"] = "1"
        if unrestricted:
            # Explicit unrestricted/Unattended authority intentionally spans the
            # host filesystem. additionalDirectories grants file access without
            # loading configuration from the added path.
            fs_root = str(Path(repo_root).anchor or "/")
            if fs_root not in additional:
                additional.append(fs_root)
        else:
            for raw in [*outside_paths, *secret_paths]:
                try:
                    target = Path(raw).expanduser().resolve(strict=False)
                    granted_dir = target if target.is_dir() else target.parent
                except (OSError, RuntimeError, ValueError):
                    continue
                text = str(granted_dir)
                if text not in additional:
                    additional.append(text)

    if not secret_relaxed:
        # Direct Claude Read-tool access to high-confidence credential stores is denied
        # without preventing authenticated developer tools (git/gh/cloud CLIs) from
        # using their credentials through their normal mechanisms.
        for rule in (
            "Read(~/.ssh/**)",
            "Read(~/.aws/**)",
            "Read(~/.config/gcloud/**)",
            "Read(~/.kube/**)",
        ):
            if rule not in deny:
                deny.append(rule)

        # Preserve narrow protection for Claude's own credential/config stores from
        # sandboxed Bash reads. The provider-configs directory is protected as a whole
        # so dynamically-created isolated gateway profiles are covered too.
        credentials = sandbox.setdefault("credentials", {})
        cred_files = credentials.setdefault("files", [])
        protected_credential_files = [
            {"path": "~/.claude/.credentials.json", "mode": "deny"},
            {"path": "~/.claude.json", "mode": "deny"},
            {"path": str(data_home() / "provider-configs"), "mode": "deny"},
        ]
        custom_config = os.environ.get("CLAUDE_CONFIG_DIR")
        if custom_config:
            protected_credential_files.append({"path": str((Path(custom_config).expanduser().resolve() / ".credentials.json")), "mode": "deny"})
        for entry in protected_credential_files:
            if entry not in cred_files:
                cred_files.append(entry)
        env_vars = credentials.setdefault("envVars", [])
        protected_env = [
            {"name": "GITHUB_TOKEN", "mode": "mask", "injectHosts": ["github.com", "api.github.com"]},
            {"name": "GH_TOKEN", "mode": "mask", "injectHosts": ["github.com", "api.github.com"]},
            {"name": "NPM_TOKEN", "mode": "mask", "injectHosts": ["registry.npmjs.org"]},
            {"name": "AWS_SECRET_ACCESS_KEY", "mode": "deny"},
            {"name": "AWS_SESSION_TOKEN", "mode": "deny"},
            {"name": "GOOGLE_APPLICATION_CREDENTIALS", "mode": "deny"},
            {"name": "AZURE_CLIENT_SECRET", "mode": "deny"},
        ]
        for entry in protected_env:
            if entry not in env_vars:
                env_vars.append(entry)

        # Protect common real-secret .env variants in every profile and add exact
        # denies for any other present .env.* files.  Sample/template names remain
        # readable in Balanced by design.
        for rule in (
            "Read(./.env.local)", "Read(./**/.env.local)",
            "Read(./.env.production)", "Read(./**/.env.production)",
            "Read(./.env.development)", "Read(./**/.env.development)",
            "Read(./.env.staging)", "Read(./**/.env.staging)",
            "Read(./.env.test)", "Read(./**/.env.test)",
            "Read(./.env.*.local)", "Read(./**/.env.*.local)",
        ):
            if rule not in deny:
                deny.append(rule)
        if repo_root:
            repo_path = Path(repo_root)
            for rule in _discovered_dotenv_read_denies(repo_path):
                if rule not in deny:
                    deny.append(rule)
            fs = sandbox.setdefault("filesystem", {})
            deny_read = fs.setdefault("denyRead", [])
            for secret_path in _discovered_dotenv_paths(repo_path):
                secret_text = str(secret_path)
                if secret_text not in deny_read:
                    deny_read.append(secret_text)

        # Strict adds broader repository secret/certificate read denials.
        if profile["strict_secret_reads"]:
            for rule in (
                "Read(./.env.*)", "Read(./**/.env.*)",
                "Read(./**/*.pem)", "Read(./**/*.p12)", "Read(./**/*.pfx)",
            ):
                if rule not in deny:
                    deny.append(rule)

    if secret_paths:
        permissions["deny"] = [
            rule for rule in permissions.get("deny", [])
            if not (isinstance(rule, str) and rule.startswith("Read("))
        ]

    if autonomy_profile == "balanced":
        excluded = sandbox.setdefault("excludedCommands", [])
        for command in _compat_excluded_commands(repo_profile):
            if command not in excluded:
                excluded.append(command)
        # A fast-forward promotion must update .git, which the inner Bash sandbox
        # intentionally protects. Run only the package-owned validator/broker
        # outside that sandbox; its matching Bash allow rule above avoids an Auto
        # classifier deadlock while the helper itself proves ancestry/state.
        broker_command = "claude-auto promote-ff *"
        if broker_command not in excluded:
            excluded.append(broker_command)

        network = sandbox.setdefault("network", {})
        allowed_domains = network.setdefault("allowedDomains", [])
        for domain in _repo_network_domains(repo_profile):
            if domain not in allowed_domains:
                allowed_domains.append(domain)
        # Preserve Claude's built-in classifier rules and add only concrete local
        # development context. Do not whitelist arbitrary external domains.
        env_rules = template.setdefault("autoMode", {}).setdefault("environment", ["$defaults"])
        if "$defaults" not in env_rules:
            env_rules.insert(0, "$defaults")
        local_rule = "Local development infrastructure: loopback/localhost services and ephemeral test containers started for this repository are non-production development infrastructure."
        if local_rule not in env_rules:
            env_rules.append(local_rule)
        if repo_root:
            repo_rule = f"Autonomous workspace: {repo_root} is the operator-selected repository root; sibling modules beneath it are one trusted engineering workspace."
            if repo_rule not in env_rules:
                env_rules.append(repo_rule)

    # Human-only runtime profile control. UserPromptSubmit is the trust boundary:
    # model output and repository text cannot invoke this hook because it fires only
    # for an actual operator prompt submitted to Claude Code.
    _append_command_hook(
        template,
        "UserPromptSubmit",
        package_root() / "hooks" / "profile_switch_prompt.py",
    )

    # Telemetry hooks let the outer supervisor distinguish classifier denials from
    # API/provider failures without scraping human-facing terminal prose.
    event_hook = package_root() / "hooks" / "runtime_event_logger.py"
    _append_command_hook(template, "PermissionDenied", event_hook)
    _append_command_hook(template, "StopFailure", event_hook)
    if repo_root and autonomy_profile != "isolated-full" and not unrestricted:
        # Enforce protected-secret and outside-read scope across Read/Grep/Glob,
        # even before a permission exception exists. This prevents broad Grep
        # from bypassing a direct Read deny on repository secret files.
        _append_command_hook(
            template,
            "PreToolUse",
            package_root() / "hooks" / "read_scope_guard.py",
            matcher="Read|Grep|Glob",
        )
    if repo_root:
        # Semantic repository authority is independent of runtime autonomy.
        # Balanced/Strict also enforce the normal host/repository boundary;
        # Isolated Full/Unattended invoke the same hook in semantic-only mode.
        _append_command_hook(
            template,
            "PreToolUse",
            package_root() / "hooks" / "write_boundary_guard.py",
            matcher="Write|Edit|NotebookEdit|Bash",
        )
    if repo_root:
        # One authoritative repository-state check after every parallel tool
        # batch. It is a cheap no-op unless P3 task authority is active.
        _append_command_hook(
            template,
            "PostToolBatch",
            package_root() / "hooks" / "task_post_batch_guard.py",
        )
    if autonomy_profile != "isolated-full" and not unrestricted:
        # Container runtimes are a host-authority boundary even in repositories
        # that do not advertise Docker/Compose files.  Apply the deterministic
        # guard to every sandboxed autonomous session; it is a no-op for ordinary
        # non-container Bash commands.
        _append_command_hook(
            template,
            "PreToolUse",
            package_root() / "hooks" / "docker_guard.py",
            matcher="Bash",
        )

    return template

def running_inside_claude() -> bool:
    return os.environ.get("CLAUDECODE") == "1"


def refuse_nested_claude_launch() -> None:
    if running_inside_claude():
        raise SystemExit(
            "claude-auto must be launched from a top-level shell, not from inside an active Claude Code session. "
            "Claude Code guards nested sessions because they share runtime resources. Open a fresh terminal and run the same claude-auto command there."
        )


def isolated_full_authorized() -> bool:
    if os.environ.get("CLAUDE_AUTO_ISOLATED_FULL") != "1":
        return False
    if Path("/.dockerenv").exists() or Path("/run/.containerenv").exists():
        return True
    return os.environ.get("CLAUDE_AUTO_ISOLATION_ATTESTATION", "").lower() in {"vm", "container", "disposable"}


def permission_mode_for_profile(
    requested: str,
    autonomy_profile: str,
    permission_overrides: set[str] | None = None,
) -> str:
    overrides = set(permission_overrides or ())
    if autonomy_profile == "unattended" or "unrestricted" in overrides:
        return "bypassPermissions"
    if "native-permissions" in overrides:
        return "bypassPermissions"
    if autonomy_profile == "isolated-full":
        if not isolated_full_authorized():
            raise SystemExit(
                "--profile isolated-full requires an explicitly attested disposable container/VM. "
                "Set CLAUDE_AUTO_ISOLATED_FULL=1 and run inside a container, or also set "
                "CLAUDE_AUTO_ISOLATION_ATTESTATION=vm|container|disposable."
            )
        return "bypassPermissions"
    return requested

def validate_repository_execution_policy(autonomy_profile: str, trust_repo_scripts: bool) -> bool:
    """Validate the supervisor-owned repository-code execution boundary."""
    if autonomy_profile == "unattended":
        return True
    trusted = bool(trust_repo_scripts)
    if autonomy_profile == "strict" and trusted:
        raise SystemExit(
            "--trust-repo-scripts is incompatible with --profile strict. "
            "Strict never permits supervisor-owned repository code to fall back to host execution."
        )
    return trusted


def make_readonly_settings(state_dir: Path, root: Path) -> dict[str, Any]:
    prof = {"repo_root": str(root.resolve()), "container_files": [], "languages": []}
    # Start from the actual Strict profile so read-only roles inherit every strict
    # secret/read boundary rather than accidentally inheriting Balanced defaults.
    template = make_settings(state_dir, "external", "strict", prof)
    template.setdefault("env", {})["CLAUDE_AUTONOMY_PROFILE"] = "readonly"
    permissions = template.setdefault("permissions", {})
    permissions["blockReadsOutsideWorkingDirectories"] = True
    deny = permissions.setdefault("deny", [])
    for tool in ("Edit", "Write", "NotebookEdit", "AskUserQuestion"):
        if tool not in deny:
            deny.append(tool)
    sandbox = template.setdefault("sandbox", {})
    sandbox["enabled"] = True
    sandbox["failIfUnavailable"] = True
    sandbox["allowUnsandboxedCommands"] = False
    fs = sandbox.setdefault("filesystem", {})
    writes = fs.setdefault("denyWrite", [])
    target = str(root.resolve())
    if target not in writes:
        writes.append(target)
    hook = package_root() / "hooks" / "readonly_guard.py"
    _append_command_hook(template, "PreToolUse", hook, matcher="Bash")
    # Keep the deterministic read-only Bash guard first for auditability and
    # backwards-compatible hook inspection; the direct write-boundary guard follows.
    entries = template.setdefault("hooks", {}).setdefault("PreToolUse", [])
    entries.sort(key=lambda x: 0 if x.get("matcher") == "Bash" else 1)
    return template


def resolve_autonomy_profile(
    explicit_profile: str | None,
    remembered_profile: str | None,
    *,
    resume_config: bool = False,
    resumed_profile: str | None = None,
) -> str:
    """Resolve a profile without silently inheriting unattended authority."""
    if explicit_profile:
        return explicit_profile
    if resume_config and resumed_profile:
        return resumed_profile
    if remembered_profile == "unattended":
        return "balanced"
    return remembered_profile or "balanced"
