"""Command line entry point.

Exit codes are the contract, because the only place this runs unattended is a
CI job:

    0   nothing at or above the threshold
    1   something at or above the threshold that no waiver covers
    2   the tool could not do its job: a file that will not load, an external
        ``$ref``, a waiver without a reason

The distinction between 1 and 2 exists so a pipeline can tell "the API changed"
from "the check is broken". Collapsing them is how a check ends up passing for
three weeks because a path was renamed.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence

from . import __version__
from .diff import diff
from .document import Document
from .errors import SpecError
from .reach import reachability
from .rules import lint, rule_names
from .variance import Severity, rule_table
from .waivers import apply_waivers, load_waivers

_SEVERITIES = [s.value for s in Severity]


def _load(path: str) -> Document:
    return Document.from_file(path)


def _emit(payload: object, text: str, as_json: bool) -> None:
    print(json.dumps(payload, indent=2, default=str) if as_json else text)


def cmd_diff(args: argparse.Namespace) -> int:
    report = diff(
        _load(args.old), _load(args.new),
        include_cosmetic=args.include_cosmetic,
        max_changes=args.max_changes,
    )
    if args.format == "json":
        print(report.to_json(indent=2, default=str))
    elif args.format == "markdown":
        print(report.to_markdown())
    else:
        print(report.to_text())
    threshold = Severity(args.fail_on)
    return 1 if report.at_least(threshold) else 0


def cmd_check(args: argparse.Namespace) -> int:
    """Diff against a baseline and let waivers absorb what was agreed."""
    report = diff(_load(args.baseline), _load(args.spec))
    threshold = Severity(args.fail_on)
    waivers = load_waivers(args.waivers) if args.waivers else []
    result = apply_waivers(report, waivers, threshold=threshold)

    if args.format == "json":
        payload = result.to_dict()
        payload["counts"] = report.counts()
        print(json.dumps(payload, indent=2, default=str))
    else:
        for change in result.remaining:
            print(change)
            print()
        for change, waiver in result.waived:
            print(f"waived    {change.code} at {change.location}\n    {waiver.reason}\n")
        for waiver in result.expired:
            print(f"expired   {waiver.code} at {waiver.location}\n    {waiver.reason}\n")
        for waiver in result.unused:
            print(f"unused    {waiver.code} at {waiver.location}")
        counts = report.counts()
        print("  ".join(f"{counts[s]} {s}" for s in _SEVERITIES))
        print(
            f"{len(result.remaining)} unwaived at or above {threshold.value}, "
            f"{len(result.waived)} waived"
        )

    if result.expired and args.strict:
        return 1
    return 1 if result.remaining else 0


def cmd_lint(args: argparse.Namespace) -> int:
    report = lint(_load(args.spec), only=args.only or None, skip=args.skip or None)
    _emit(report.to_dict(), report.to_text(), args.format == "json")
    threshold = Severity(args.fail_on)
    if report.crashed:
        return 2
    return 1 if report.at_least(threshold) else 0


def cmd_reach(args: argparse.Namespace) -> int:
    result = reachability(_load(args.spec))
    if args.format == "json":
        print(json.dumps(result.to_dict(), indent=2))
        return 0
    for label, names in result.to_dict().items():
        print(f"{label} ({len(names)})")
        for name in names:
            print(f"    {name}")
    return 0


def cmd_rules(args: argparse.Namespace) -> int:
    if args.format == "json":
        print(json.dumps(
            {
                "schema_rules": [
                    {
                        "code": r.code,
                        "request": r.request.value,
                        "response": r.response.value,
                        "why": r.why,
                    }
                    for r in rule_table()
                ],
                "lint_rules": rule_names(),
            },
            indent=2,
        ))
        return 0
    width = max(len(r.code) for r in rule_table())
    print(f"{'code'.ljust(width)}  {'request':9}  {'response':9}")
    for entry in rule_table():
        print(f"{entry.code.ljust(width)}  {entry.request.value:9}  {entry.response.value:9}")
    print("\nlint rules: " + ", ".join(rule_names()))
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    document = _load(args.spec)
    operations = list(document.operations())
    payload = {
        "title": document.title,
        "version": document.version,
        "paths": len({op.path for op in operations}),
        "operations": len(operations),
        "components": len(document.schemas()),
        "nodes": document.count_nodes(),
    }
    _emit(
        payload,
        "\n".join(f"{k:12} {v}" for k, v in payload.items()),
        args.format == "json",
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pexafy-openapi",
        description="diff, lint and gate an OpenAPI description",
    )
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_format(p: argparse.ArgumentParser, choices: Sequence[str]) -> None:
        p.add_argument("--format", choices=list(choices), default=choices[0])

    p_diff = subparsers.add_parser("diff", help="classify every difference between two documents")
    p_diff.add_argument("old")
    p_diff.add_argument("new")
    add_format(p_diff, ("text", "json", "markdown"))
    p_diff.add_argument("--include-cosmetic", action="store_true",
                        help="report description and example churn as well")
    p_diff.add_argument("--fail-on", choices=_SEVERITIES, default="breaking")
    p_diff.add_argument("--max-changes", type=int, default=2000)
    p_diff.set_defaults(func=cmd_diff)

    p_check = subparsers.add_parser("check", help="gate a document against a released baseline")
    p_check.add_argument("spec")
    p_check.add_argument("--baseline", required=True)
    p_check.add_argument("--waivers")
    add_format(p_check, ("text", "json"))
    p_check.add_argument("--fail-on", choices=_SEVERITIES, default="breaking")
    p_check.add_argument("--strict", action="store_true", help="expired waivers fail the run")
    p_check.set_defaults(func=cmd_check)

    p_lint = subparsers.add_parser("lint", help="check one document against the publishing rules")
    p_lint.add_argument("spec")
    add_format(p_lint, ("text", "json"))
    p_lint.add_argument("--only", action="append", default=[])
    p_lint.add_argument("--skip", action="append", default=[])
    p_lint.add_argument("--fail-on", choices=_SEVERITIES, default="breaking")
    p_lint.set_defaults(func=cmd_lint)

    p_reach = subparsers.add_parser(
        "reach", help="group components by the side of the call they are reached from"
    )
    p_reach.add_argument("spec")
    add_format(p_reach, ("text", "json"))
    p_reach.set_defaults(func=cmd_reach)

    p_rules = subparsers.add_parser("rules", help="print the classification table")
    add_format(p_rules, ("text", "json"))
    p_rules.set_defaults(func=cmd_rules)

    p_stats = subparsers.add_parser("stats", help="size of a document")
    p_stats.add_argument("spec")
    add_format(p_stats, ("text", "json"))
    p_stats.set_defaults(func=cmd_stats)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except SpecError as exc:
        print(f"pexafy-openapi: {exc}", file=sys.stderr)
        return 2
    except BrokenPipeError:  # pragma: no cover - only when piped into head
        return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
