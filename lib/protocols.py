from __future__ import annotations

import json
import re
from typing import Any


def parse_json_protocol(text: str, prefix: str) -> dict[str, Any] | None:
    """Parse the last JSON protocol record, tolerating pretty/multiline model output."""
    marker = prefix + ":"
    starts = [m.end() for m in re.finditer(re.escape(marker), text)]
    for start in reversed(starts):
        tail = text[start:]
        brace = tail.find("{")
        if brace < 0:
            continue
        depth = 0
        in_string = False
        escaped = False
        buf: list[str] = []
        for ch in tail[brace:]:
            if in_string:
                if escaped:
                    buf.append(ch)
                    escaped = False
                    continue
                if ch == "\\":
                    buf.append(ch)
                    escaped = True
                    continue
                if ch == '"':
                    buf.append(ch)
                    in_string = False
                    continue
                if ch == "\n":
                    buf.append("\\n")
                    continue
                if ch == "\r":
                    buf.append("\\r")
                    continue
                if ch == "\t":
                    buf.append("\\t")
                    continue
                buf.append(ch)
                continue
            if ch == '"':
                in_string = True
                buf.append(ch)
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
            buf.append(ch)
            if depth == 0 and buf:
                try:
                    obj = json.loads("".join(buf))
                except json.JSONDecodeError:
                    break
                if isinstance(obj, dict):
                    return obj
                break
    return None


def parse_plan_impact(text: str) -> tuple[str, str | None, bool]:
    impacts = re.findall(r"AUTONOMY_PLAN_IMPACT:\s*(NONE|LOCAL|MATERIAL|REQUIREMENT)\b", text, flags=re.I)
    impact = impacts[-1].upper() if impacts else "NONE"
    changes = re.findall(r"AUTONOMY_PLAN_CHANGE:\s*(.+)", text)
    change = changes[-1].strip()[:1800] if changes else None
    phase = bool(re.findall(r"AUTONOMY_PHASE_BOUNDARY:\s*YES\b", text, flags=re.I))
    return impact, change, phase


def parse_progress_checkpoint(text: str) -> dict[str, Any] | None:
    obj = parse_json_protocol(text, "AUTONOMY_PROGRESS")
    if not isinstance(obj, dict):
        return None
    out: dict[str, Any] = {}
    for key in ("completed_task_ids", "verification", "external_checkpoint", "remaining_task_ids"):
        value = obj.get(key)
        if isinstance(value, (str, list, dict, int, float, bool)) or value is None:
            out[key] = value
    return out or None


def parse_status(text: str) -> tuple[str, str | None]:
    status_matches = re.findall(r"AUTONOMY_STATUS:\s*(CONTINUE|COMPLETE|BLOCKED)\b", text, flags=re.I)
    status = status_matches[-1].upper() if status_matches else "CONTINUE"
    summaries = re.findall(r"AUTONOMY_SUMMARY:\s*(.+)", text)
    summary = summaries[-1].strip()[:1000] if summaries else None
    return status, summary


def has_explicit_status(text: str) -> bool:
    return bool(re.search(r"AUTONOMY_STATUS:\s*(CONTINUE|COMPLETE|BLOCKED)\b", text, flags=re.I))


def extract_result_json(stdout: str) -> tuple[str, str | None, dict[str, Any] | None]:
    stdout = stdout.strip()
    if not stdout:
        return "", None, None
    try:
        obj = json.loads(stdout)
        if isinstance(obj, dict):
            result = obj.get("result")
            if isinstance(result, str):
                return result, obj.get("session_id"), obj
            for key in ("content", "message", "text"):
                if isinstance(obj.get(key), str):
                    return obj[key], obj.get("session_id"), obj
    except Exception:
        pass
    return stdout, None, None


def parse_challenger(text: str) -> tuple[str, str | None]:
    verdicts = re.findall(r"CHALLENGER_VERDICT:\s*(PASS|FAIL|STALE)\b", text, flags=re.I)
    verdict = verdicts[-1].upper() if verdicts else "FAIL"
    summaries = re.findall(r"CHALLENGER_SUMMARY:\s*(.+)", text)
    summary = summaries[-1].strip()[:1800] if summaries else (text[-1500:].strip() or None)
    return verdict, summary


def parse_security_review_gate(text: str) -> dict[str, Any]:
    """Parse the deterministic adjudication protocol layered over native /security-review."""
    matches = re.findall(r"SECURITY_REVIEW_GATE:\s*(\{.*\})", text, flags=re.I)
    if not matches:
        return {
            "verdict": "BLOCKED",
            "summary": "Security-review adjudicator did not emit the required SECURITY_REVIEW_GATE object.",
            "findings": [],
        }
    try:
        obj = json.loads(matches[-1])
    except Exception:
        return {
            "verdict": "BLOCKED",
            "summary": "Security-review adjudicator emitted malformed SECURITY_REVIEW_GATE JSON.",
            "findings": [],
        }
    verdict = str(obj.get("verdict", "BLOCKED")).upper()
    if verdict not in {"PASS", "FAIL", "BLOCKED"}:
        verdict = "BLOCKED"
    findings = obj.get("findings") if isinstance(obj.get("findings"), list) else []
    findings = [str(x)[:1800] for x in findings][:30]
    summary = str(obj.get("summary", ""))[:2400]
    if verdict == "FAIL" and not findings:
        findings = [summary or "Native security review reported a material issue requiring remediation."]
    return {"verdict": verdict, "summary": summary, "findings": findings}


def parse_runtime_verify_gate(text: str) -> dict[str, Any]:
    obj = parse_json_protocol(text, "RUNTIME_VERIFY_GATE")
    if not isinstance(obj, dict):
        return {
            "verdict": "SKIP",
            "summary": "Runtime verification adjudicator did not emit RUNTIME_VERIFY_GATE.",
            "findings": [],
        }
    verdict = str(obj.get("verdict", "SKIP")).upper()
    if verdict not in {"PASS", "FAIL", "SKIP"}:
        verdict = "SKIP"
    findings = obj.get("findings") if isinstance(obj.get("findings"), list) else []
    return {
        "verdict": verdict,
        "summary": str(obj.get("summary", ""))[:2000],
        "findings": [str(x)[:1800] for x in findings][:20],
    }


def parse_code_review_gate(text: str) -> dict[str, Any]:
    obj = parse_json_protocol(text, "CODE_REVIEW_GATE")
    if not isinstance(obj, dict):
        return {
            "verdict": "BLOCKED",
            "summary": "Correctness reviewer did not emit CODE_REVIEW_GATE.",
            "findings": [],
        }
    verdict = str(obj.get("verdict", "BLOCKED")).upper()
    if verdict not in {"PASS", "FAIL", "BLOCKED"}:
        verdict = "BLOCKED"
    return {
        "verdict": verdict,
        "summary": str(obj.get("summary", ""))[:2000],
        "findings": obj.get("findings") if isinstance(obj.get("findings"), list) else [],
    }


def parse_qualification(text: str) -> dict[str, Any] | None:
    matches = re.findall(r"MODEL_QUALIFICATION:\s*(\{.*\})\s*$", text, flags=re.M)
    if not matches:
        return None
    try:
        obj = json.loads(matches[-1])
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def parse_permission_request(text: str) -> dict[str, Any] | None:
    obj = parse_json_protocol(text, "AUTONOMY_PERMISSION_REQUEST")
    if not isinstance(obj, dict):
        return None
    capability = str(obj.get("capability") or "").strip().lower()
    allowed = {
        "native-permissions",
        "outside-repository",
        "container-host-authority",
        "secret-read",
        "host-repository-execution",
        "unrestricted",
    }
    if capability not in allowed:
        capability = "native-permissions"

    def clean(key: str, limit: int = 2000) -> str:
        return str(obj.get(key) or "").strip()[:limit]

    return {
        "capability": capability,
        "operation": clean("operation"),
        "resource": clean("resource"),
        "why_needed": clean("why_needed"),
        "risk": clean("risk"),
        "safer_alternative": clean("safer_alternative"),
    }
