#!/usr/bin/env python3
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
import hashlib
import json
import os
import shutil
import shlex
import sys
from pathlib import Path
from typing import Any

PACKAGE_ID = "claude-autonomous-optimisation-pack"
MARKER_NAME = ".claude-auto-integration.json"
HOOK_TAG = "CLAUDE_AUTO_GLOBAL_SESSION_CONTEXT"
PROMPT_HOOK_TAG = "CLAUDE_AUTO_HUMAN_PROFILE_SWITCH"


def claude_home() -> Path:
    raw = os.environ.get("CLAUDE_CONFIG_DIR")
    if raw:
        return Path(raw).expanduser().resolve()
    return (Path.home() / ".claude").resolve()


def package_root_from_arg(value: str | None) -> Path:
    if value:
        return Path(value).expanduser().resolve()
    return Path(__file__).resolve().parent.parent


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(value, f, indent=2, sort_keys=True)
        f.write("\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def file_sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def copy_owned_tree(src: Path, dst: Path) -> list[str]:
    if dst.exists() and not dst.is_dir():
        raise SystemExit(f"Refusing to replace non-directory user customization path: {dst}")
    dst.mkdir(parents=True, exist_ok=True)
    try:
        dst.chmod(0o700)
    except OSError:
        pass
    installed: list[str] = []
    for root, dirs, files in os.walk(src):
        rootp = Path(root)
        rel = rootp.relative_to(src)
        target_root = dst / rel
        target_root.mkdir(parents=True, exist_ok=True)
        try:
            target_root.chmod(0o700)
        except OSError:
            pass
        for d in dirs:
            p = target_root / d
            p.mkdir(parents=True, exist_ok=True)
            try:
                p.chmod(0o700)
            except OSError:
                pass
        for name in files:
            s = rootp / name
            t = target_root / name
            # Dedicated package-owned subtrees. If a file already exists but isn't
            # recorded as package-owned, refuse instead of clobbering it.
            shutil.copy2(s, t)
            try:
                t.chmod(0o600)
            except OSError:
                pass
            installed.append(str(t))
    return installed


def hook_object(package_root: Path) -> dict[str, Any]:
    script = package_root / "hooks" / "global_session_context.py"
    command = f"python3 -B {shlex.quote(str(script))} # {HOOK_TAG}"
    return {
        "hooks": [{"type": "command", "command": command, "timeout": 8}],
    }


def profile_hook_object(package_root: Path) -> dict[str, Any]:
    script = package_root / "hooks" / "profile_switch_prompt.py"
    command = f"python3 -B {shlex.quote(str(script))} # {PROMPT_HOOK_TAG}"
    return {
        "hooks": [{"type": "command", "command": command, "timeout": 8}],
    }


def remove_our_hook(settings: dict[str, Any]) -> bool:
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return False
    changed = False
    for event, tag in (("SessionStart", HOOK_TAG), ("UserPromptSubmit", PROMPT_HOOK_TAG)):
        arr = hooks.get(event)
        if not isinstance(arr, list):
            continue
        kept = []
        for entry in arr:
            text = json.dumps(entry, sort_keys=True) if isinstance(entry, dict) else str(entry)
            if tag in text:
                changed = True
                continue
            kept.append(entry)
        if kept:
            hooks[event] = kept
        else:
            hooks.pop(event, None)
    if changed and not hooks:
        settings.pop("hooks", None)
    return changed


def _mappings(package_root: Path, ch: Path) -> list[tuple[Path, Path]]:
    return [
        (package_root / "templates" / "user" / "rules" / "claude-auto", ch / "rules" / "claude-auto"),
        (package_root / "templates" / "user" / "agents" / "claude-auto", ch / "agents" / "claude-auto"),
        (package_root / "templates" / "user" / "skills" / "claude-auto", ch / "skills" / "claude-auto"),
        (package_root / "templates" / "user" / "skills" / "profile", ch / "skills" / "profile"),
    ]


def _expected_owned_files(mappings: list[tuple[Path, Path]]) -> list[str]:
    out: list[str] = []
    for src, dst in mappings:
        if not src.exists():
            raise SystemExit(f"Package template is missing: {src}")
        for root, _, files in os.walk(src):
            rootp = Path(root)
            rel = rootp.relative_to(src)
            for name in files:
                out.append(str((dst / rel / name).resolve(strict=False)))
    return sorted(out)


def _path_in_any_namespace(path: Path, namespaces: list[Path]) -> bool:
    rp = path.resolve(strict=False)
    for ns in namespaces:
        rn = ns.resolve(strict=False)
        try:
            if rp.is_relative_to(rn):
                return True
        except AttributeError:  # Python < 3.9 compatibility guard; package requires 3.10+
            try:
                rp.relative_to(rn)
                return True
            except ValueError:
                pass
    return False


def _prune_empty_namespace(ns: Path) -> None:
    if not ns.exists() or not ns.is_dir() or ns.is_symlink():
        return
    for root, dirs, _ in os.walk(ns, topdown=False):
        rootp = Path(root)
        for d in dirs:
            p = rootp / d
            try:
                if p.is_dir() and not p.is_symlink():
                    p.rmdir()
            except OSError:
                pass
    try:
        ns.rmdir()
    except OSError:
        pass


def install_user_layer(package_root: Path) -> dict[str, Any]:
    ch = claude_home()
    ch.mkdir(parents=True, exist_ok=True)
    try:
        ch.chmod(0o700)
    except OSError:
        pass
    marker = ch / MARKER_NAME
    settings_path = ch / "settings.json"
    try:
        settings = load_json(settings_path, {})
    except Exception as exc:
        raise SystemExit(f"Refusing to modify invalid Claude user settings at {settings_path}: {exc}")
    if not isinstance(settings, dict):
        raise SystemExit(f"Refusing to modify non-object Claude user settings at {settings_path}")
    existing_hooks = settings.get("hooks")
    if existing_hooks is not None and not isinstance(existing_hooks, dict):
        raise SystemExit(f"Refusing to modify malformed hooks object in {settings_path}")
    if isinstance(existing_hooks, dict):
        existing_session = existing_hooks.get("SessionStart")
        if existing_session is not None and not isinstance(existing_session, list):
            raise SystemExit(f"Refusing to modify malformed SessionStart hooks in {settings_path}")
        existing_prompt = existing_hooks.get("UserPromptSubmit")
        if existing_prompt is not None and not isinstance(existing_prompt, list):
            raise SystemExit(f"Refusing to modify malformed UserPromptSubmit hooks in {settings_path}")

    existing_marker = load_json(marker, {}) if marker.exists() else {}
    if existing_marker:
        if existing_marker.get("package_id") != PACKAGE_ID:
            raise SystemExit(f"Refusing to adopt foreign Claude integration marker at {marker}")
        recorded_home = existing_marker.get("claude_home")
        if recorded_home and Path(recorded_home).resolve() != ch:
            raise SystemExit("Refusing to update user layer: marker Claude-home mismatch")
    install_uuid = existing_marker.get("install_uuid") or os.urandom(16).hex()
    version = (package_root / "VERSION").read_text(encoding="utf-8").strip()

    # Copy only into dedicated namespaced subtrees. Refuse to adopt a pre-existing
    # namespace unless our marker proves a prior installation owned it.
    mappings = _mappings(package_root, ch)
    namespaces = [dst.resolve(strict=False) for _, dst in mappings]
    marker_owned = bool(existing_marker.get("package_id") == PACKAGE_ID and existing_marker.get("install_uuid"))
    if not marker_owned:
        conflicts = [str(dst) for _, dst in mappings if dst.exists()]
        if conflicts:
            raise SystemExit("Refusing to overwrite pre-existing Claude user customization paths: " + ", ".join(conflicts))

    expected_files = _expected_owned_files(mappings)
    # Write a recovery marker before mutation. If the process is interrupted, rerun
    # can safely repair the same package-owned namespaces or uninstall can clean them.
    provisional = {
        "package_id": PACKAGE_ID,
        "version": version,
        "state": "installing",
        "install_uuid": install_uuid,
        "package_root": str(package_root),
        "claude_home": str(ch),
        "settings_path": str(settings_path),
        "hook_tag": HOOK_TAG,
        "prompt_hook_tag": PROMPT_HOOK_TAG,
        "owned_files": expected_files,
        "owned_dirs": [str(x) for x in namespaces],
    }
    write_json(marker, provisional)

    installed_files: list[str] = []
    try:
        for src, dst in mappings:
            installed_files.extend(copy_owned_tree(src, dst))

        # Remove files owned by the previous package version that are no longer part
        # of this version. Never trust marker paths outside the exact namespaces.
        current = {str(Path(x).resolve(strict=False)) for x in installed_files}
        for raw in existing_marker.get("owned_files", []):
            p = Path(raw)
            rp = str(p.resolve(strict=False))
            if rp not in current and _path_in_any_namespace(p, namespaces):
                try:
                    if p.is_symlink() or p.is_file():
                        p.unlink()
                except OSError:
                    pass

        # Add package-owned SessionStart + human-only profile-control hooks.
        # Preserve every unrelated setting/hook.
        remove_our_hook(settings)
        hooks = settings.setdefault("hooks", {})
        session = hooks.setdefault("SessionStart", [])
        session.append(hook_object(package_root))
        prompt = hooks.setdefault("UserPromptSubmit", [])
        prompt.append(profile_hook_object(package_root))
        write_json(settings_path, settings)

        payload = {
            **provisional,
            "state": "installed",
            "owned_files": sorted(str(Path(x).resolve(strict=False)) for x in installed_files),
            "file_sha256": {str(Path(x).resolve(strict=False)): file_sha(Path(x)) for x in installed_files},
        }
        write_json(marker, payload)
        return payload
    except BaseException:
        # Keep the provisional marker so a subsequent install/remove can recover
        # safely rather than misclassifying the namespaced files as foreign.
        raise


def remove_user_layer() -> dict[str, Any]:
    ch = claude_home()
    marker = ch / MARKER_NAME
    if not marker.exists():
        return {"removed": False, "reason": "marker-not-found", "claude_home": str(ch)}
    obj = load_json(marker, {})
    if obj.get("package_id") != PACKAGE_ID:
        raise SystemExit(f"Refusing to remove user layer: marker identity mismatch at {marker}")
    if Path(obj.get("claude_home", "")).resolve() != ch:
        raise SystemExit("Refusing to remove user layer: marker Claude-home mismatch")

    settings_path = ch / "settings.json"
    if settings_path.exists():
        try:
            settings = load_json(settings_path, {})
            if isinstance(settings, dict) and remove_our_hook(settings):
                write_json(settings_path, settings)
        except Exception as exc:
            raise SystemExit(f"Refusing partial uninstall because Claude user settings cannot be safely edited: {exc}")

    # Never trust arbitrary marker directory paths. Derive the only valid namespaces
    # from the current Claude home and remove exact recorded package-owned files.
    mappings = _mappings(package_root_from_arg(obj.get("package_root")), ch)
    namespaces = [dst.resolve(strict=False) for _, dst in mappings]
    for raw in obj.get("owned_files", []):
        p = Path(raw)
        if not _path_in_any_namespace(p, namespaces):
            continue
        try:
            if p.is_symlink() or p.is_file():
                p.unlink()
        except OSError:
            pass
    for ns in namespaces:
        _prune_empty_namespace(ns)
    marker.unlink(missing_ok=True)
    return {"removed": True, "claude_home": str(ch)}

def status() -> dict[str, Any]:
    ch = claude_home()
    marker = ch / MARKER_NAME
    settings_path = ch / "settings.json"
    obj = load_json(marker, {}) if marker.exists() else {}
    settings = load_json(settings_path, {}) if settings_path.exists() else {}
    settings_blob = json.dumps(settings, sort_keys=True)
    hook_present = HOOK_TAG in settings_blob and PROMPT_HOOK_TAG in settings_blob
    files = obj.get("owned_files", []) if isinstance(obj, dict) else []
    return {
        "claude_home": str(ch),
        "installed": bool(obj.get("package_id") == PACKAGE_ID and obj.get("state") == "installed"),
        "version": obj.get("version"),
        "hook_present": hook_present,
        "owned_files_present": all(Path(p).exists() for p in files) if files else False,
        "marker": str(marker),
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="claude-auto-user-layer")
    sp = p.add_subparsers(dest="cmd", required=True)
    q = sp.add_parser("install")
    q.add_argument("--package-root")
    sp.add_parser("remove")
    sp.add_parser("status")
    args = p.parse_args(argv)
    if args.cmd == "install":
        print(json.dumps(install_user_layer(package_root_from_arg(args.package_root)), indent=2))
        return 0
    if args.cmd == "remove":
        print(json.dumps(remove_user_layer(), indent=2))
        return 0
    if args.cmd == "status":
        print(json.dumps(status(), indent=2))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
