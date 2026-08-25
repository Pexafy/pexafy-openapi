"""What runs in CI before a release: diff against the baseline, apply waivers.

    python examples/gate_a_release.py

Prints the markdown a bot would post on the pull request and exits non-zero if
anything breaking survived the waiver file.
"""

from __future__ import annotations

import sys
from pathlib import Path

from pexafy_openapi import Document, apply_waivers, diff, load_waivers

REPO = Path(__file__).resolve().parent.parent


def main() -> int:
    baseline = Document.from_file(REPO / "openapi" / "releases" / "1.2.0.json")
    candidate = Document.from_file(REPO / "openapi" / "v1.json")

    report = diff(baseline, candidate)
    print(report.to_markdown())

    result = apply_waivers(report, load_waivers(REPO / "waivers.json"))
    for change, waiver in result.waived:
        print(f"\nwaived {change.code} at {change.location}\n  {waiver.reason}")
    for waiver in result.expired:
        print(f"\nexpired waiver {waiver.code}; it no longer covers anything")
    for waiver in result.unused:
        print(f"\nunused waiver {waiver.code}; delete it or fix its location glob")

    if result.remaining:
        print(f"\n{len(result.remaining)} breaking changes nobody accepted")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
