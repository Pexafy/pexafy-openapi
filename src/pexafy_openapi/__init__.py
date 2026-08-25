"""The Pexafy API described in OpenAPI, and the tools that keep the description honest.

    >>> from pexafy_openapi import Document, diff
    >>> report = diff(Document.from_file("openapi/releases/1.2.0.json"),
    ...               Document.from_file("openapi/v1.json"))
    >>> [c.code for c in report.breaking]
    ['enum.value.added']
"""

from __future__ import annotations

__version__ = "1.3.0"

from .diff import Change, DiffReport, diff
from .document import Document, Operation, escape, inline_refs, resolve_pointer, unescape
from .errors import BudgetExceeded, LoadError, PointerError, RefError, SpecError
from .reach import Reachability, reachability
from .rules import Finding, LintReport, lint, rule_names
from .variance import ALL_RULES, Rule, Severity, Variance, rule_table, severity_of
from .waivers import Waiver, WaiverResult, apply_waivers, load_waivers, parse_waivers

__all__ = [
    "ALL_RULES",
    "BudgetExceeded",
    "Change",
    "DiffReport",
    "Document",
    "Finding",
    "LintReport",
    "LoadError",
    "Operation",
    "PointerError",
    "Reachability",
    "RefError",
    "Rule",
    "Severity",
    "SpecError",
    "Variance",
    "Waiver",
    "WaiverResult",
    "__version__",
    "apply_waivers",
    "diff",
    "escape",
    "inline_refs",
    "lint",
    "load_waivers",
    "parse_waivers",
    "reachability",
    "resolve_pointer",
    "rule_names",
    "rule_table",
    "severity_of",
    "unescape",
]
