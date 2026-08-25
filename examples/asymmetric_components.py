"""Find the components where one edit has two correct answers.

A component reached from both a request and a response is judged twice, under
opposite rules. Those are the ones worth splitting in two before somebody
approves an edit having seen half the consequence.

    python examples/asymmetric_components.py
"""

from __future__ import annotations

from pathlib import Path

from pexafy_openapi import ALL_RULES, Document, Severity, reachability

REPO = Path(__file__).resolve().parent.parent


def main() -> None:
    document = Document.from_file(REPO / "openapi" / "v1.json")
    result = reachability(document)

    print(f"{len(result.both)} components are read from both sides:")
    for name in sorted(result.both):
        print(f"  {name}")

    print("\nedits whose verdict flips with the side of the call:")
    for rule in sorted(ALL_RULES.values(), key=lambda r: r.code):
        if rule.request is rule.response:
            continue
        if Severity.BREAKING not in (rule.request, rule.response):
            continue
        print(f"  {rule.code:34} request {rule.request.value:9} response {rule.response.value}")


if __name__ == "__main__":
    main()
