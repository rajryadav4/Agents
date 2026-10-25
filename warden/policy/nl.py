"""Natural-language → policy compiler.

Turns plain English such as

    "Require 2 approvals for refunds over $500"
    "Never send email outside acme.com"
    "Block shell commands containing 'rm -rf'"
    "Only the research agent may fetch web pages"
    "Quarantine any agent that posts to pastebin.com"

into a validated policy rule. It is deterministic (no LLM needed), explains what
it understood, and reports a confidence so humans review uncertain rules.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# keyword → tool name. Longer phrases are matched first.
TOOL_SYNONYMS: dict[str, str] = {
    "delete files": "delete_file", "delete file": "delete_file", "deleting files": "delete_file",
    "remove files": "delete_file", "deletes": "delete_file", "delete": "delete_file",
    "write files": "write_file", "write file": "write_file", "writing files": "write_file", "modify files": "write_file",
    "read files": "read_file", "read file": "read_file", "reading files": "read_file",
    "shell commands": "shell_exec", "shell command": "shell_exec", "shell": "shell_exec", "terminal": "shell_exec",
    "commands": "shell_exec", "command": "shell_exec", "bash": "shell_exec", "run code": "shell_exec",
    "send emails": "send_email", "send email": "send_email", "sending email": "send_email", "emails": "send_email",
    "email": "send_email", "e-mail": "send_email", "mail": "send_email",
    "refunds": "issue_refund", "refund": "issue_refund", "payments": "issue_refund", "payment": "issue_refund",
    "web pages": "web_fetch", "web page": "web_fetch", "websites": "web_fetch", "website": "web_fetch",
    "browse": "web_fetch", "fetch": "web_fetch", "the web": "web_fetch", "internet": "web_fetch", "urls": "web_fetch",
    "http requests": "http_post", "http request": "http_post", "upload": "http_post", "uploads": "http_post",
    "post data": "http_post", "webhooks": "http_post", "webhook": "http_post",
    "slack": "slack_post", "post to slack": "slack_post", "slack messages": "slack_post",
    "database": "db_query", "db": "db_query", "customer records": "db_query", "customer data": "db_query",
    "query": "db_query", "queries": "db_query", "sql": "db_query",
    "secrets": "read_secret", "secret": "read_secret", "credentials": "read_secret", "api keys": "read_secret",
    "messages to other agents": "message_agent", "message other agents": "message_agent", "talk to other agents": "message_agent",
    "knowledge base": "kb_search",
}

# The main argument each tool's free-text constraints apply to.
PRIMARY_ARG = {
    "shell_exec": "cmd", "send_email": "body", "web_fetch": "url", "http_post": "url", "slack_post": "text",
    "db_query": "sql", "write_file": "path", "delete_file": "path", "read_file": "path", "read_secret": "name",
    "issue_refund": "amount", "message_agent": "text", "kb_search": "query",
}
DEST_ARG = {"send_email": "to", "web_fetch": "url", "http_post": "url", "slack_post": "channel",
            "message_agent": "to", "write_file": "path", "delete_file": "path", "read_file": "path"}
AMOUNT_ARG = {"issue_refund": "amount"}

_NUM_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5}


@dataclass
class Compiled:
    rule: dict[str, Any] | None
    confidence: float
    understood: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"rule": self.rule, "confidence": round(self.confidence, 2), "understood": self.understood,
                "warnings": self.warnings}


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return "nl-" + "-".join(s.split("-")[:6])


def _find_tools(text: str, known: set[str] | None) -> list[str]:
    found: list[str] = []
    t = text.lower()
    # explicit tool names written as identifiers
    for ident in re.findall(r"\b[a-z]+_[a-z_]+\b", t):
        if known is None or ident in known:
            if ident not in found:
                found.append(ident)
    masked = t
    for phrase in sorted(TOOL_SYNONYMS, key=len, reverse=True):
        pattern = r"\b" + re.escape(phrase) + r"\b"
        if re.search(pattern, masked):
            tool = TOOL_SYNONYMS[phrase]
            if (known is None or tool in known) and tool not in found:
                found.append(tool)
            masked = re.sub(pattern, " ", masked)
    return found


def _find_agent(text: str) -> tuple[str | None, bool]:
    """Return (agent glob, only=True if 'only X may ...')."""
    t = text.lower()
    only = bool(re.search(r"\bonly\b", t))
    m = re.search(r"\b(?:the\s+)?([a-z][a-z0-9_-]*?)(?:[- ]?(?:agent|bot))\b", t)
    if m and m.group(1) not in {"any", "an", "a", "every", "all", "each", "other", "the", "no"}:
        name = m.group(1).strip("-_")
        return f"{name}*", only
    m = re.search(r"\b([a-z]+-bot)\b", t)
    if m:
        return m.group(1), only
    return None, only


def compile_rule(sentence: str, known_tools: list[str] | None = None, org_domain: str | None = None) -> Compiled:
    text = sentence.strip()
    low = text.lower()
    understood: list[str] = []
    warnings: list[str] = []
    confidence = 0.0
    known = set(known_tools) if known_tools else None

    # ---- action
    approvals = 1
    m = re.search(r"\b(\d+|one|two|three|four|five)\s+(?:person|people|human|humans|approvals?|approvers?|sign-?offs?)", low)
    if m:
        approvals = int(m.group(1)) if m.group(1).isdigit() else _NUM_WORDS[m.group(1)]
    if re.search(r"\bquarantine|isolate|honeypot\b", low):
        action = "quarantine"
    elif re.search(r"approv|sign-?off|ask (me|a human|someone)|confirm with|human review|check with", low):
        action = "require_approval"
    elif re.search(r"\b(warn|alert|flag|notify)\b", low):
        action = "warn"
    elif re.search(r"\b(never|block|deny|forbid|prevent|prohibit|disallow|don't|do not|must not|cannot|can't|no agent|stop|only)\b", low):
        action = "deny"
    else:
        action = "deny"
        warnings.append("no explicit action word found; defaulting to deny")
        confidence -= 0.2
    understood.append(f"action: {action}" + (f" ({approvals} approvals)" if action == "require_approval" else ""))
    confidence += 0.3

    mode = "enforce"
    if re.search(r"\b(observe|monitor only|log only|dry[- ]run|shadow mode|just watch)\b", low):
        mode = "observe"
        understood.append("mode: observe (records but does not block)")

    # ---- tools
    tools = _find_tools(low, known)
    when: dict[str, Any] = {}
    if tools:
        when["tool"] = tools[0] if len(tools) == 1 else tools
        understood.append(f"tools: {', '.join(tools)}")
        confidence += 0.35
    else:
        warnings.append("could not tell which tool this is about; rule applies to every tool")

    primary_tool = tools[0] if tools else None
    args: dict[str, Any] = {}

    # ---- amounts
    m = re.search(r"(over|above|more than|greater than|exceeding|larger than|bigger than|at least|under|below|less than|smaller than)\s*\$?\s*([\d,]+(?:\.\d+)?)\s*(k|thousand)?", low)
    if m:
        amount = float(m.group(2).replace(",", ""))
        if m.group(3):
            amount *= 1000
        op = {"at least": "gte"}.get(m.group(1), "gt" if m.group(1) in {"over", "above", "more than", "greater than", "exceeding", "larger than", "bigger than"} else "lt")
        arg = AMOUNT_ARG.get(primary_tool or "", "amount")
        args[arg] = {op: amount if not amount.is_integer() else int(amount)}
        understood.append(f"{arg} {op} {args[arg][op]}")
        confidence += 0.15

    # ---- destinations: outside a domain / to a domain
    m = re.search(r"(?:outside(?: of)?|external to|not (?:at|in|from)|except)\s+(?:the\s+)?(?:@)?([a-z0-9-]+(?:\.[a-z0-9-]+)+)", low)
    external_kw = re.search(r"\b(external|outside the (?:company|org|organisation|organization)|third[- ]part(?:y|ies))\b", low)
    dest_arg = DEST_ARG.get(primary_tool or "", "to")
    if m:
        domain = m.group(1)
        args[dest_arg] = {"not_glob": f"*@{domain}" if dest_arg == "to" else f"*{domain}*"}
        understood.append(f"{dest_arg} outside {domain}")
        confidence += 0.15
    elif external_kw and org_domain:
        args[dest_arg] = {"not_glob": f"*@{org_domain}" if dest_arg == "to" else f"*{org_domain}*"}
        understood.append(f"{dest_arg} outside {org_domain} (your org)")
        confidence += 0.1
    elif external_kw:
        warnings.append("'external' needs an org domain; pass org_domain to resolve it")
    else:
        m = re.search(r"\b(?:to|on|at)\s+([a-z0-9-]+(?:\.[a-z0-9-]+)+)\b", low)
        if m and "@" not in m.group(1):
            args[dest_arg] = {"contains": m.group(1)}
            understood.append(f"{dest_arg} contains {m.group(1)}")
            confidence += 0.15

    # ---- quoted / 'containing' content
    quoted = re.findall(r"[\"'“‘`]([^\"'”’`]{2,})[\"'”’`]", text)
    m = re.search(r"\b(?:containing|contain|contains|that include|including|with the word|mentioning|matching)\s+([^\"'“‘`][^,.;]*)", low)
    needles = quoted or ([m.group(1).strip()] if m else [])
    if needles and primary_tool:
        arg = PRIMARY_ARG.get(primary_tool, "text")
        args.setdefault(arg, {})
        if isinstance(args[arg], dict):
            args[arg]["contains"] = needles if len(needles) > 1 else needles[0]
        understood.append(f"{arg} contains {needles}")
        confidence += 0.15

    if args:
        when["args"] = args

    # ---- agent scoping
    agent, only = _find_agent(low)
    if agent:
        if only and action == "deny":
            when["agent"] = {"not_glob": agent}
            understood.append(f"applies to every agent except {agent}")
        else:
            when["agent"] = agent
            understood.append(f"applies to agent {agent}")
        confidence += 0.1

    # ---- time
    if re.search(r"outside (?:of )?(?:business|working|office) hours|after hours|at night", low):
        when["time"] = {"outside": "09:00-18:00"}
        understood.append("time: outside 09:00-18:00 UTC")
        confidence += 0.1

    severity = {"quarantine": "critical", "deny": "high", "require_approval": "medium", "warn": "low"}[action]
    if not when:
        return Compiled(rule=None, confidence=0.0, understood=understood,
                        warnings=warnings + ["nothing to match on; refusing to create a rule that matches every call"])

    rule = {"id": _slug(text), "description": text, "when": when, "action": action, "severity": severity, "mode": mode}
    if action == "require_approval":
        rule["approvals"] = approvals
    return Compiled(rule=rule, confidence=max(0.0, min(1.0, confidence)), understood=understood, warnings=warnings)
