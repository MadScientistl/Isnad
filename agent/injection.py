"""Telemetry-only detector for instruction-like text inside tool-result strings.

It NEVER changes permissions or behaviour: flags are only attached to trace/audit events. The real
defences are structural (intersection permissions, evidence/id validation, human approval).
"""
from __future__ import annotations

import re

_PATTERNS = {
    "ignore_instructions": re.compile(
        r"(ignore|disregard|forget|override)\s+(all\s+|any\s+|the\s+)?(previous|prior|above|earlier|system)\s+"
        r"(instructions?|prompts?|rules?|messages?)", re.I),
    "new_instructions": re.compile(r"\b(new|updated)\s+(instructions?|system prompt)\b", re.I),
    "must_call": re.compile(r"\byou\s+(must|should|have to|need to)\s+(now\s+)?(call|run|execute|use|invoke|propose)\b", re.I),
    "names_tool": re.compile(r"\b(call|invoke|use|run)\s+(the\s+)?(propose_action|run_function|query_objects)\b", re.I),
    "role_impersonation": re.compile(r"(^|\n)\s*(system|assistant|developer|admin(istrator)?)\s*:", re.I),
    "act_as": re.compile(r"\b(act|behave|respond)\s+as\s+(an?\s+)?(admin(istrator)?|system|root|another role)\b", re.I),
    "you_are_now": re.compile(r"\byou\s+are\s+now\b", re.I),
    "pay_or_approve": re.compile(r"\b(pay_invoice|approve (this|the) proposal)\b", re.I),
}


def scan(obj) -> list[str]:
    """Sorted unique flag names for every instruction-like string found anywhere in `obj`."""
    found: set[str] = set()

    def walk(x):
        if isinstance(x, str):
            for name, rx in _PATTERNS.items():
                if rx.search(x):
                    found.add(name)
        elif isinstance(x, dict):
            for v in x.values():
                walk(v)
        elif isinstance(x, (list, tuple)):
            for v in x:
                walk(v)

    walk(obj)
    return sorted(found)
