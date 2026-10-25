"""Condition language used by policy rules.

A ``when`` block is a dict whose keys must *all* match:

    tool:  "send_email" | ["send_*", "slack_post"] | {op: value}
    agent: "research-*" | {not_glob: "finance-*"}
    role:  "support"
    args:  {"to": {"not_glob": "*@acme.com"}, "amount": {"gt": 500}}
    time:  {"outside": "09:00-18:00"}            (UTC)
    any:   [when, when, ...]
    all:   [when, when, ...]
    not:   when

Operators: eq ne in not_in glob not_glob regex not_regex gt gte lt lte
           contains not_contains starts_with ends_with exists len_gt len_lt
When an argument is a list, a positive operator matches if *any* element matches;
a negative operator (not_*) matches if *any* element violates it (e.g. one
external recipient is enough to trigger "to not_glob *@acme.com").
"""
from __future__ import annotations

import fnmatch
import re
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any

OPERATORS = {
    "eq", "ne", "in", "not_in", "glob", "not_glob", "regex", "not_regex", "gt", "gte", "lt", "lte",
    "contains", "not_contains", "starts_with", "ends_with", "exists", "len_gt", "len_lt",
}
NEGATIVE = {"ne", "not_in", "not_glob", "not_regex", "not_contains"}
MISSING = object()


class ConditionError(ValueError):
    pass


@lru_cache(maxsize=1024)
def _compile(pattern: str) -> re.Pattern:
    try:
        return re.compile(pattern, re.IGNORECASE | re.DOTALL)
    except re.error as exc:
        raise ConditionError(f"invalid regex {pattern!r}: {exc}") from exc


def _num(v: Any) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v.replace(",", "").replace("$", "").strip())
        except ValueError:
            return None
    return None


def _positive(op: str, value: Any, expected: Any) -> bool:
    s = "" if value is None else str(value)
    if op == "eq":
        return value == expected or (isinstance(expected, str) and s.lower() == expected.lower())
    if op == "in":
        return value in expected or s.lower() in {str(e).lower() for e in expected}
    if op == "glob":
        pats = expected if isinstance(expected, list) else [expected]
        return any(fnmatch.fnmatch(s.lower(), str(p).lower()) for p in pats)
    if op == "regex":
        return bool(_compile(str(expected)).search(s))
    if op == "contains":
        needles = expected if isinstance(expected, list) else [expected]
        return any(str(n).lower() in s.lower() for n in needles)
    if op == "starts_with":
        return s.lower().startswith(str(expected).lower())
    if op == "ends_with":
        return s.lower().endswith(str(expected).lower())
    if op in ("gt", "gte", "lt", "lte"):
        a, b = _num(value), _num(expected)
        if a is None or b is None:
            return False
        return {"gt": a > b, "gte": a >= b, "lt": a < b, "lte": a <= b}[op]
    if op == "len_gt":
        return len(s) > int(expected)
    if op == "len_lt":
        return len(s) < int(expected)
    raise ConditionError(f"unknown operator {op!r}")


_NEG_TO_POS = {"ne": "eq", "not_in": "in", "not_glob": "glob", "not_regex": "regex", "not_contains": "contains"}


def match_op(op: str, value: Any, expected: Any) -> bool:
    if op not in OPERATORS:
        raise ConditionError(f"unknown operator {op!r}")
    if op == "exists":
        return (value is not MISSING and value is not None) == bool(expected)
    if value is MISSING:
        return False
    values = value if isinstance(value, (list, tuple)) else [value]
    if not values:
        return False
    if op in NEGATIVE:
        pos = _NEG_TO_POS[op]
        return any(not _positive(pos, v, expected) for v in values)
    return any(_positive(op, v, expected) for v in values)


def match_spec(spec: Any, value: Any) -> bool:
    """A spec is a literal/glob string, a list of globs, or an {op: expected} dict."""
    if isinstance(spec, dict):
        return all(match_op(op, value, exp) for op, exp in spec.items())
    if isinstance(spec, list):
        return match_op("glob", value, [str(s) for s in spec])
    if isinstance(spec, str):
        return match_op("glob", value, spec)
    return match_op("eq", value, spec)


def get_path(obj: Any, path: str) -> Any:
    cur = obj
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return MISSING
    return cur


def _parse_window(window: str) -> tuple[int, int]:
    try:
        start, end = window.split("-")
        sh, sm = (int(x) for x in start.strip().split(":"))
        eh, em = (int(x) for x in end.strip().split(":"))
    except ValueError as exc:
        raise ConditionError(f"invalid time window {window!r}, expected HH:MM-HH:MM") from exc
    return sh * 60 + sm, eh * 60 + em


def _in_window(minutes: int, window: str) -> bool:
    start, end = _parse_window(window)
    if start <= end:
        return start <= minutes < end
    return minutes >= start or minutes < end  # overnight window


def evaluate(when: dict[str, Any] | None, ctx: dict[str, Any]) -> bool:
    """ctx keys: tool, agent, role, args, now (unix ts)."""
    if not when:
        return True
    if not isinstance(when, dict):
        raise ConditionError("'when' must be a mapping")
    for key, spec in when.items():
        if key == "tool":
            if not match_spec(spec, ctx.get("tool")):
                return False
        elif key == "agent":
            if not match_spec(spec, ctx.get("agent")):
                return False
        elif key == "role":
            if not match_spec(spec, ctx.get("role")):
                return False
        elif key == "args":
            if not isinstance(spec, dict):
                raise ConditionError("'args' must map argument paths to specs")
            for path, arg_spec in spec.items():
                value = get_path(ctx.get("args") or {}, path)
                if isinstance(arg_spec, dict) and set(arg_spec) <= OPERATORS:
                    if not all(match_op(op, value, exp) for op, exp in arg_spec.items()):
                        return False
                elif value is MISSING or not match_spec(arg_spec, value):
                    return False
        elif key == "time":
            now = ctx.get("now")
            dt = datetime.fromtimestamp(now, tz=timezone.utc) if now else datetime.now(timezone.utc)
            minutes = dt.hour * 60 + dt.minute
            if "outside" in spec and _in_window(minutes, spec["outside"]):
                return False
            if "within" in spec and not _in_window(minutes, spec["within"]):
                return False
            if "weekdays" in spec and dt.strftime("%a").lower()[:3] not in [d.lower()[:3] for d in spec["weekdays"]]:
                return False
        elif key == "any":
            if not any(evaluate(w, ctx) for w in spec):
                return False
        elif key == "all":
            if not all(evaluate(w, ctx) for w in spec):
                return False
        elif key == "not":
            if evaluate(spec, ctx):
                return False
        else:
            raise ConditionError(f"unknown condition key {key!r}")
    return True


def validate(when: Any, path: str = "when") -> list[str]:
    """Static validation used by the linter. Returns a list of problems."""
    problems: list[str] = []
    if when is None:
        return problems
    if not isinstance(when, dict):
        return [f"{path}: must be a mapping"]
    for key, spec in when.items():
        if key in ("tool", "agent", "role"):
            problems += _validate_spec(spec, f"{path}.{key}")
        elif key == "args":
            if not isinstance(spec, dict):
                problems.append(f"{path}.args: must be a mapping")
                continue
            for arg, s in spec.items():
                problems += _validate_spec(s, f"{path}.args.{arg}")
        elif key == "time":
            if not isinstance(spec, dict):
                problems.append(f"{path}.time: must be a mapping")
                continue
            for k in ("outside", "within"):
                if k in spec:
                    try:
                        _parse_window(spec[k])
                    except ConditionError as exc:
                        problems.append(f"{path}.time.{k}: {exc}")
        elif key in ("any", "all"):
            if not isinstance(spec, list):
                problems.append(f"{path}.{key}: must be a list")
                continue
            for i, w in enumerate(spec):
                problems += validate(w, f"{path}.{key}[{i}]")
        elif key == "not":
            problems += validate(spec, f"{path}.not")
        else:
            problems.append(f"{path}: unknown condition key {key!r}")
    return problems


def _validate_spec(spec: Any, path: str) -> list[str]:
    if isinstance(spec, dict):
        out = []
        for op, exp in spec.items():
            if op not in OPERATORS:
                out.append(f"{path}: unknown operator {op!r}")
            elif op in ("regex", "not_regex"):
                try:
                    _compile(str(exp))
                except ConditionError as exc:
                    out.append(f"{path}: {exc}")
            elif op in ("gt", "gte", "lt", "lte") and _num(exp) is None:
                out.append(f"{path}: operator {op!r} needs a number, got {exp!r}")
        return out
    return []
