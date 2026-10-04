from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class StateCorruptionError(RuntimeError):
    pass


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass
    return path


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def json_dump(path: Path, obj: Any) -> None:
    """Crash-safer private JSON write. state.json retains a previous generation and checksum."""
    _ensure_private_dir(path.parent)
    payload_obj = obj
    if path.name == "state.json" and isinstance(obj, dict):
        payload_obj = dict(obj)
        payload_obj["state_generation"] = int(obj.get("state_generation", 0) or 0) + 1
        payload_obj["state_saved_at"] = _utcnow()
        obj.update({"state_generation": payload_obj["state_generation"], "state_saved_at": payload_obj["state_saved_at"]})
        if path.exists():
            try:
                current = json.loads(path.read_text())
                prev = path.with_name("state.prev.json")
                prev_tmp = prev.with_suffix(prev.suffix + ".tmp")
                with prev_tmp.open("w") as fh:
                    fh.write(json.dumps(current, indent=2, sort_keys=True) + "\n")
                    fh.flush()
                    os.fsync(fh.fileno())
                prev_tmp.replace(prev)
                prev.chmod(0o600)
                prev_payload = json.dumps(current, indent=2, sort_keys=True) + "\n"
                prev_side = path.with_name("state.prev.sha256")
                prev_side.write_text(hashlib.sha256(prev_payload.encode()).hexdigest() + "  state.prev.json\n")
                try:
                    prev_side.chmod(0o600)
                except OSError:
                    pass
            except Exception:
                pass
    payload = json.dumps(payload_obj, indent=2, sort_keys=True) + "\n"
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w") as fh:
        fh.write(payload)
        fh.flush()
        os.fsync(fh.fileno())
    try:
        tmp.chmod(0o600)
    except OSError:
        pass
    tmp.replace(path)
    try:
        path.chmod(0o600)
    except OSError:
        pass
    if path.name == "state.json":
        digest = hashlib.sha256(payload.encode()).hexdigest()
        side = path.with_name("state.sha256")
        with side.open("w") as fh:
            fh.write(digest + "  state.json\n")
            fh.flush()
            os.fsync(fh.fileno())
        try:
            side.chmod(0o600)
        except OSError:
            pass
        journal = path.with_name("state.journal.jsonl")
        with journal.open("a") as fh:
            fh.write(json.dumps({
                "generation": payload_obj.get("state_generation"),
                "sha256": digest,
                "saved_at": payload_obj.get("state_saved_at"),
            }, separators=(",", ":")) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        try:
            journal.chmod(0o600)
        except OSError:
            pass
    _fsync_dir(path.parent)


def _verify_state_checksum(path: Path, raw: str) -> bool:
    side = path.with_name("state.sha256")
    if not side.exists():
        return True
    try:
        expected = side.read_text().strip().split()[0]
        return expected == hashlib.sha256(raw.encode()).hexdigest()
    except Exception:
        return False


def _quarantine_corrupt_state(path: Path) -> None:
    """Preserve the last rejected state without letting it become state.prev."""
    corrupt = path.with_name("state.corrupt.last.json")
    corrupt_side = path.with_name("state.corrupt.last.sha256")
    side = path.with_name("state.sha256")
    try:
        if path.exists():
            os.replace(path, corrupt)
            try:
                corrupt.chmod(0o600)
            except OSError:
                pass
        if side.exists():
            os.replace(side, corrupt_side)
            try:
                corrupt_side.chmod(0o600)
            except OSError:
                pass
        _fsync_dir(path.parent)
    except OSError:
        # If quarantine itself fails, remove only the known-bad current generation
        # before recovery so json_dump cannot rotate it into state.prev.
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        try:
            side.unlink(missing_ok=True)
        except OSError:
            pass


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        raw = path.read_text()
        obj = json.loads(raw)
        if path.name == "state.json" and not _verify_state_checksum(path, raw):
            raise ValueError("state checksum mismatch")
        return obj
    except Exception as exc:
        if path.name != "state.json":
            return default
        prev = path.with_name("state.prev.json")
        try:
            prev_raw = prev.read_text()
            prev_side = path.with_name("state.prev.sha256")
            if prev_side.exists():
                expected_prev = prev_side.read_text().strip().split()[0]
                if expected_prev != hashlib.sha256(prev_raw.encode()).hexdigest():
                    raise ValueError("previous state checksum mismatch")
            recovered = json.loads(prev_raw)
        except Exception as prev_exc:
            raise StateCorruptionError(
                f"Durable state is corrupt and no valid previous generation is available: {path}: {exc}"
            ) from prev_exc
        recovered["state_recovered_at"] = _utcnow()
        recovered["state_recovery_reason"] = str(exc)[:500]
        _quarantine_corrupt_state(path)
        json_dump(path, recovered)
        return recovered


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
