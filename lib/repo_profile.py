from __future__ import annotations

import json
import os
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from environment_policy import sanitised_subprocess_env
from process_runner import run
from repo_identity import repo_id
from runtime_paths import utcnow


def iter_files_limited(root: Path, max_depth: int = 6, max_files: int = 20000) -> Iterable[Path]:
    """Filesystem fallback for non-Git projects."""
    skip_dirs = {
        ".git", "node_modules", "vendor", "target", "dist", "build", ".next", ".venv", "venv",
        "__pycache__", ".tox", ".mypy_cache", ".pytest_cache", ".idea", ".vscode", "coverage"
    }
    count = 0
    root_depth = len(root.parts)
    for dirpath, dirnames, filenames in os.walk(root):
        d = Path(dirpath)
        depth = len(d.parts) - root_depth
        dirnames[:] = [x for x in dirnames if x not in skip_dirs and not x.startswith(".cache")]
        if depth >= max_depth:
            dirnames[:] = []
        for fn in filenames:
            count += 1
            if count > max_files:
                return
            yield d / fn


def iter_repo_files(root: Path, max_files: int = 100000) -> Iterable[Path]:
    cp = subprocess.run(
        ["git", "-C", str(root), "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        capture_output=True,
        env=sanitised_subprocess_env(),
    )
    if cp.returncode == 0:
        count = 0
        for raw in cp.stdout.split(b"\0"):
            if not raw:
                continue
            rel = raw.decode("utf-8", errors="surrogateescape")
            p = root / rel
            if p.is_file():
                count += 1
                if count > max_files:
                    return
                yield p
        return
    yield from iter_files_limited(root)

LANG_BY_SUFFIX = {
    ".py": "python", ".pyi": "python",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".tsx": "typescript",
    ".rs": "rust", ".go": "go", ".java": "java", ".kt": "kotlin", ".kts": "kotlin",
    ".c": "c", ".h": "c-cpp", ".cc": "c-cpp", ".cpp": "c-cpp", ".cxx": "c-cpp", ".hpp": "c-cpp",
    ".cs": "csharp", ".php": "php", ".rb": "ruby", ".lua": "lua", ".swift": "swift",
    ".sh": "shell", ".bash": "shell", ".zsh": "shell", ".tf": "terraform"
}

MANIFESTS = {
    "pyproject.toml": "python", "requirements.txt": "python", "Pipfile": "python", "poetry.lock": "python", "uv.lock": "python",
    "package.json": "javascript-typescript", "pnpm-lock.yaml": "javascript-typescript", "yarn.lock": "javascript-typescript", "bun.lockb": "javascript-typescript",
    "Cargo.toml": "rust", "go.mod": "go", "pom.xml": "java", "build.gradle": "java-kotlin", "build.gradle.kts": "java-kotlin",
    "Gemfile": "ruby", "composer.json": "php", "Package.swift": "swift", "CMakeLists.txt": "c-cpp", "*.csproj": "csharp"
}

LSP_MAP = {
    "python": "pyright-lsp",
    "javascript": "typescript-lsp",
    "typescript": "typescript-lsp",
    "javascript-typescript": "typescript-lsp",
    "rust": "rust-analyzer-lsp",
    "go": "gopls-lsp",
    "java": "jdtls-lsp",
    "c": "clangd-lsp",
    "c-cpp": "clangd-lsp",
    "ruby": "ruby-lsp",
    "lua": "lua-lsp",
    "php": "php-lsp",
    "csharp": "csharp-lsp",
    "kotlin": "kotlin-lsp",
    "swift": "swift-lsp",
}

BASE_PLUGINS = ["claude-code-setup", "context7", "serena"]
DEFERRED_PLUGINS = ["session-report", "claude-md-management"]
# Official LSP plugins are useful candidates, but as of 2026-09-28 an upstream
# marketplace packaging/registration issue remains open. They are never installed
# automatically unless the operator explicitly opts in with --include-lsp.



VERIFICATION_CONTRACT_PATH = ".claude-auto/verification.json"
_VERIFICATION_CATEGORIES = ("format", "lint", "typecheck", "test", "build")
MAX_DECLARED_VERIFICATION_COMMANDS = 256
MAX_DECLARED_VERIFICATION_COMMAND_LENGTH = 16_384


def load_declared_verification_commands(root: Path) -> dict[str, list[str]]:
    """Load mandatory repository-owned verification commands, failing closed."""
    path = root / VERIFICATION_CONTRACT_PATH
    out: dict[str, list[str]] = {key: [] for key in _VERIFICATION_CATEGORIES}
    if not path.exists() and not path.is_symlink():
        return out

    # This file is an executable verification control surface. It must be owned
    # by the repository itself, not redirected through a symlink (including a
    # symlinked .claude-auto directory) to mutable external state.
    control_dir = root / ".claude-auto"
    if path.is_symlink() or control_dir.is_symlink():
        raise ValueError(
            f"{VERIFICATION_CONTRACT_PATH} must be a regular repository-owned file, not a symlink"
        )
    try:
        resolved_root = root.resolve()
        resolved_path = path.resolve(strict=True)
        resolved_path.relative_to(resolved_root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError(
            f"{VERIFICATION_CONTRACT_PATH} must resolve inside the repository"
        ) from exc
    if not resolved_path.is_file():
        raise ValueError(f"{VERIFICATION_CONTRACT_PATH} must be a regular file")

    try:
        obj = json.loads(resolved_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"Invalid {VERIFICATION_CONTRACT_PATH}: {exc}") from exc
    if not isinstance(obj, dict):
        raise ValueError(f"{VERIFICATION_CONTRACT_PATH} must contain a JSON object")
    if obj.get("schema_version") != 1:
        raise ValueError(f"{VERIFICATION_CONTRACT_PATH} schema_version must be 1")
    commands = obj.get("commands")
    if not isinstance(commands, dict) or not commands:
        raise ValueError(f"{VERIFICATION_CONTRACT_PATH}.commands must be a non-empty object")
    unknown = sorted(set(commands) - set(_VERIFICATION_CATEGORIES))
    if unknown:
        raise ValueError(
            f"{VERIFICATION_CONTRACT_PATH} contains unsupported categories: "
            + ", ".join(unknown)
        )
    total = 0
    for category in _VERIFICATION_CATEGORIES:
        rows = commands.get(category, [])
        if rows is None:
            rows = []
        if not isinstance(rows, list):
            raise ValueError(f"{VERIFICATION_CONTRACT_PATH}.commands.{category} must be a list")
        seen: set[str] = set()
        for index, raw in enumerate(rows):
            if not isinstance(raw, str) or not raw.strip():
                raise ValueError(
                    f"{VERIFICATION_CONTRACT_PATH}.commands.{category}[{index}] "
                    "must be a non-empty string"
                )
            command = raw.strip()
            if len(command) > MAX_DECLARED_VERIFICATION_COMMAND_LENGTH:
                raise ValueError(
                    f"{VERIFICATION_CONTRACT_PATH}.commands.{category}[{index}] "
                    f"exceeds {MAX_DECLARED_VERIFICATION_COMMAND_LENGTH} characters"
                )
            if command in seen:
                raise ValueError(
                    f"{VERIFICATION_CONTRACT_PATH}.commands.{category} contains duplicate command: {command}"
                )
            seen.add(command)
            out[category].append(command)
            total += 1
            if total > MAX_DECLARED_VERIFICATION_COMMANDS:
                raise ValueError(
                    f"{VERIFICATION_CONTRACT_PATH} declares more than "
                    f"{MAX_DECLARED_VERIFICATION_COMMANDS} mandatory commands"
                )
    if total == 0:
        raise ValueError(f"{VERIFICATION_CONTRACT_PATH} declares no verification commands")
    return out


def detect_commands(root: Path, present: set[str]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {"format": [], "lint": [], "typecheck": [], "test": [], "build": []}
    names = {p.name for p in root.iterdir()} if root.exists() else set()

    if "package.json" in names:
        try:
            pkg = json.loads((root / "package.json").read_text())
            scripts = pkg.get("scripts", {}) if isinstance(pkg, dict) else {}
            pm = "npm"
            if (root / "pnpm-lock.yaml").exists(): pm = "pnpm"
            elif (root / "yarn.lock").exists(): pm = "yarn"
            elif (root / "bun.lockb").exists() or (root / "bun.lock").exists(): pm = "bun"
            prefix = {"npm": "npm run", "pnpm": "pnpm", "yarn": "yarn", "bun": "bun run"}[pm]
            for k in scripts:
                lk = k.lower()
                cmd = f"{prefix} {k}"
                if lk in {"test", "tests", "test:unit"}: out["test"].append(cmd)
                if lk in {"lint", "lint:check"}: out["lint"].append(cmd)
                if lk in {"typecheck", "type-check", "check:types"}: out["typecheck"].append(cmd)
                if lk in {"build", "compile"}: out["build"].append(cmd)
                if lk in {"format", "fmt", "format:check"}: out["format"].append(cmd)
        except Exception:
            pass

    if "python" in present:
        if (root / "pyproject.toml").exists():
            txt = (root / "pyproject.toml").read_text(errors="ignore")[:200_000]
            if "[tool.pytest" in txt or "pytest" in txt: out["test"].append("pytest")
            if "[tool.ruff" in txt or "ruff" in txt: out["lint"].append("ruff check .")
            if "[tool.black" in txt or "black" in txt: out["format"].append("black --check .")
            if "[tool.mypy" in txt or "mypy" in txt: out["typecheck"].append("mypy .")
            if "pyright" in txt: out["typecheck"].append("pyright")
        elif (root / "pytest.ini").exists() or (root / "tests").exists():
            out["test"].append("pytest")

    if "rust" in present and (root / "Cargo.toml").exists():
        out["format"].append("cargo fmt --check")
        out["lint"].append("cargo clippy --all-targets --all-features -- -D warnings")
        out["test"].append("cargo test --all-features")
        out["build"].append("cargo check --all-targets --all-features")

    if "go" in present and (root / "go.mod").exists():
        out["format"].append("gofmt -l .")
        out["test"].append("go test ./...")
        out["build"].append("go build ./...")

    # Additional mainstream ecosystems. Prefer repository wrappers when present
    # so verification uses the project's pinned toolchain.
    if (root / "gradlew").exists() or (root / "build.gradle").exists() or (root / "build.gradle.kts").exists():
        gradle = (
            "./gradlew"
            if (root / "gradlew").exists() and os.access(root / "gradlew", os.X_OK)
            else "bash ./gradlew"
            if (root / "gradlew").exists()
            else "gradle"
        )
        out["test"].append(f"{gradle} test")
        out["build"].append(f"{gradle} build -x test")

    if (root / "mvnw").exists() or (root / "pom.xml").exists():
        mvn = (
            "./mvnw"
            if (root / "mvnw").exists() and os.access(root / "mvnw", os.X_OK)
            else "bash ./mvnw"
            if (root / "mvnw").exists()
            else "mvn"
        )
        out["test"].append(f"{mvn} test")
        out["build"].append(f"{mvn} -DskipTests package")

    dotnet_projects = list(root.glob("*.sln")) + list(root.glob("*.csproj"))
    if dotnet_projects:
        target = shlex.quote(dotnet_projects[0].name)
        out["test"].append(f"dotnet test {target} --no-restore")
        out["build"].append(f"dotnet build {target} --no-restore")

    if (root / "tox.ini").exists():
        out["test"].append("tox")
    if (root / "noxfile.py").exists():
        out["test"].append("nox")

    if (root / "CMakeLists.txt").exists():
        out["test"].append(
            "cmake -S . -B .claude-auto-build && "
            "cmake --build .claude-auto-build && "
            "ctest --test-dir .claude-auto-build --output-on-failure"
        )

    if (root / "Rakefile").exists():
        ruby = "bundle exec rake" if (root / "Gemfile").exists() else "rake"
        out["test"].append(ruby)

    if (root / "Makefile").exists(): out["build"].append("make")
    if (root / "justfile").exists() or (root / "Justfile").exists(): out["build"].append("just --list")

    # Repository-declared commands are mandatory and are read from the live
    # repository every time the profile is refreshed. Malformed contracts fail
    # closed instead of silently falling back to generic heuristics.
    declared = load_declared_verification_commands(root)
    for category, commands in declared.items():
        # Mandatory repository checks are never truncated. Bound only generic
        # discovery so "mandatory" cannot silently mean "first N".
        generic = [cmd for cmd in out[category] if cmd not in commands]
        out[category] = list(dict.fromkeys(commands + generic[:24]))
    return out


@dataclass
class RepoProfile:
    schema_version: int
    generated_at: str
    repo_root: str
    repo_id: str
    git_repo: bool
    git_branch: str | None
    git_head: str | None
    languages: list[str]
    language_counts: dict[str, int]
    manifests: list[str]
    repo_instruction_files: list[str]
    ci_files: list[str]
    container_files: list[str]
    build_test_hints: dict[str, list[str]]
    recommended_plugins: list[str]
    lsp_candidates: list[str]
    existing_claude_config: list[str]


def profile_repo(root: Path) -> RepoProfile:
    counts: dict[str, int] = {}
    manifests: set[str] = set()
    instructions: set[str] = set()
    ci_files: set[str] = set()
    container_files: set[str] = set()
    existing: set[str] = set()

    instruction_names = {"CLAUDE.md", "AGENTS.md", "CONTRIBUTING.md", "README.md", "DEVELOPING.md", "DEVELOPMENT.md"}
    for p in iter_repo_files(root):
        rel = p.relative_to(root).as_posix()
        suf = p.suffix.lower()
        if suf in LANG_BY_SUFFIX:
            lang = LANG_BY_SUFFIX[suf]
            counts[lang] = counts.get(lang, 0) + 1
        if p.name in MANIFESTS or any(p.match(pattern) for pattern in MANIFESTS if "*" in pattern):
            manifests.add(rel)
        if p.name in instruction_names and len(p.relative_to(root).parts) <= 3:
            instructions.add(rel)
        if rel == VERIFICATION_CONTRACT_PATH:
            instructions.add(rel)
        if rel.startswith(".github/workflows/") or rel in {".gitlab-ci.yml", "Jenkinsfile", "azure-pipelines.yml", "bitbucket-pipelines.yml"}:
            ci_files.add(rel)
        if p.name in {"Dockerfile", "docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"} or p.name.startswith("Dockerfile."):
            container_files.add(rel)
        if rel == ".claude/settings.json" or rel == ".claude/settings.local.json" or rel.startswith(".claude/agents/") or rel.startswith(".claude/skills/"):
            existing.add(rel)

    # Manifest-only language signals.
    for rel in list(manifests):
        name = Path(rel).name
        lang = MANIFESTS.get(name)
        if lang and lang not in counts:
            counts[lang] = 1

    langs = sorted(counts, key=lambda x: (-counts[x], x))
    plugins = list(BASE_PLUGINS)
    lsp_candidates: list[str] = []
    for lang in langs:
        plug = LSP_MAP.get(lang)
        if plug and plug not in lsp_candidates:
            lsp_candidates.append(plug)

    branch_cp = run(["git", "-C", str(root), "branch", "--show-current"])
    head_cp = run(["git", "-C", str(root), "rev-parse", "HEAD"])
    is_git = head_cp.returncode == 0
    return RepoProfile(
        schema_version=1,
        generated_at=utcnow(),
        repo_root=str(root),
        repo_id=repo_id(root),
        git_repo=is_git,
        git_branch=branch_cp.stdout.strip() or None if is_git else None,
        git_head=head_cp.stdout.strip() or None if is_git else None,
        languages=langs,
        language_counts=counts,
        manifests=sorted(manifests),
        repo_instruction_files=sorted(instructions),
        ci_files=sorted(ci_files),
        container_files=sorted(container_files),
        build_test_hints=detect_commands(root, set(langs)),
        recommended_plugins=plugins,
        lsp_candidates=lsp_candidates,
        existing_claude_config=sorted(existing),
    )
