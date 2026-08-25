"""How often the additive/removal heuristic is wrong, and how often it is dangerous.

Most breaking-change detection reduces to one heuristic: adding things is safe,
removing things breaks people. It is right often enough to feel right, and the
cases where it is wrong are the cases that cost money, because a heuristic that
says "safe" is a heuristic nobody reviews.

This measures the disagreement instead of asserting it. Every edit in the rule
table is applied twice to a working document, once to a schema reached from a
request and once to the same schema reached from a response, and the diff is
run for real. Nothing is looked up in the table: the numbers come out of the
same code path a user gets.

Three outcomes are counted per edit:

  agree             the heuristic and the variance rules give the same verdict
  wrong, loud       the heuristic says breaking where it is not; noise, and the
                    reason people turn these tools off
  wrong, quiet      the heuristic says safe where it breaks consumers; this is
                    the column that matters

Run it:

    python bench/bench_variance.py --out bench/results-variance.csv
"""

from __future__ import annotations

import argparse
import copy
import csv
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from pexafy_openapi import Document, Severity, diff  # noqa: E402

FIELDS = ["edit", "position", "variance_verdict", "heuristic_verdict", "outcome", "code"]


def _base() -> dict[str, Any]:
    """One component, reachable from a request body and from a response body.

    Deliberately the same component on both sides. That is the case the
    heuristic cannot get right even in principle: there is one edit and two
    correct answers, so any classifier that does not know which side it is
    looking at has to pick one and be wrong on the other.
    """
    subject = {
        "type": "object",
        "required": ["kind"],
        "properties": {
            "kind": {"type": "string", "enum": ["alpha", "beta"]},
            "label": {"type": "string", "maxLength": 100},
            "count": {"type": "integer", "format": "int32", "minimum": 0, "maximum": 100},
            "note": {"type": "string"},
        },
    }
    return {
        "openapi": "3.1.0",
        "info": {"title": "variance", "version": "1.0.0"},
        "paths": {
            "/thing": {
                "post": {
                    "operationId": "createThing",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {"schema": {"$ref": "#/components/schemas/Subject"}}
                        },
                    },
                    "responses": {"201": {"description": "made"}},
                },
                "get": {
                    "operationId": "getThing",
                    "responses": {
                        "200": {
                            "description": "ok",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/Echo"}
                                }
                            },
                        }
                    },
                },
            }
        },
        "components": {
            "schemas": {
                "Subject": copy.deepcopy(subject),
                "Echo": copy.deepcopy(subject),
            }
        },
    }


def _target(document: dict[str, Any], position: str) -> dict[str, Any]:
    return document["components"]["schemas"]["Subject" if position == "request" else "Echo"]


Edit = Callable[[dict[str, Any]], None]


def _add_required(schema: dict[str, Any]) -> None:
    schema["properties"]["extra"] = {"type": "string"}
    schema["required"] = [*schema["required"], "extra"]


def _add_optional(schema: dict[str, Any]) -> None:
    schema["properties"]["extra"] = {"type": "string"}


def _remove_property(schema: dict[str, Any]) -> None:
    schema["properties"].pop("note")


def _make_required(schema: dict[str, Any]) -> None:
    schema["required"] = [*schema["required"], "label"]


def _make_optional(schema: dict[str, Any]) -> None:
    schema["required"] = [r for r in schema["required"] if r != "kind"]


def _add_enum_value(schema: dict[str, Any]) -> None:
    schema["properties"]["kind"]["enum"] = ["alpha", "beta", "gamma"]


def _remove_enum_value(schema: dict[str, Any]) -> None:
    schema["properties"]["kind"]["enum"] = ["alpha"]


def _widen_type(schema: dict[str, Any]) -> None:
    schema["properties"]["count"]["type"] = ["integer", "string"]


def _narrow_type(schema: dict[str, Any]) -> None:
    schema["properties"]["label"] = {"type": "string", "maxLength": 100, "enum": ["one"]}


def _add_null(schema: dict[str, Any]) -> None:
    schema["properties"]["label"]["type"] = ["string", "null"]


def _relax_bound(schema: dict[str, Any]) -> None:
    schema["properties"]["label"]["maxLength"] = 4000


def _tighten_bound(schema: dict[str, Any]) -> None:
    schema["properties"]["label"]["maxLength"] = 10


def _close_extra_keys(schema: dict[str, Any]) -> None:
    schema["additionalProperties"] = False


def _change_format(schema: dict[str, Any]) -> None:
    schema["properties"]["count"]["format"] = "int64"


#: (name, edit, how the additive/removal heuristic reads it).
#: The heuristic is charitable to itself: anything that only adds is called
#: safe, anything that removes or restricts is called breaking. That is the
#: strongest version of it, and it is still wrong where it is wrong.
EDITS: list[tuple[str, Edit, str]] = [
    ("add required property", _add_required, "safe"),
    ("add optional property", _add_optional, "safe"),
    ("remove property", _remove_property, "breaking"),
    ("optional becomes required", _make_required, "breaking"),
    ("required becomes optional", _make_optional, "safe"),
    ("add enum value", _add_enum_value, "safe"),
    ("remove enum value", _remove_enum_value, "breaking"),
    ("widen type", _widen_type, "safe"),
    ("narrow type", _narrow_type, "breaking"),
    ("allow null", _add_null, "safe"),
    ("relax maxLength", _relax_bound, "safe"),
    ("tighten maxLength", _tighten_bound, "breaking"),
    ("forbid unknown keys", _close_extra_keys, "breaking"),
    ("int32 to int64", _change_format, "safe"),
]


def run() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for name, edit, heuristic in EDITS:
        for position in ("request", "response"):
            before = _base()
            after = _base()
            edit(_target(after, position))
            report = diff(Document.from_dict(before), Document.from_dict(after))
            relevant = [
                c for c in report.changes
                if c.variance and c.variance.value == position
            ]
            worst = max(
                (c.severity for c in relevant), default=Severity.COSMETIC,
                key=lambda s: s.rank,
            )
            code = ",".join(sorted({c.code for c in relevant})) or "none"
            verdict = "breaking" if worst is Severity.BREAKING else "safe"
            if verdict == heuristic:
                outcome = "agree"
            elif heuristic == "breaking":
                outcome = "wrong, loud"
            else:
                outcome = "wrong, quiet"
            rows.append({
                "edit": name,
                "position": position,
                "variance_verdict": verdict,
                "heuristic_verdict": heuristic,
                "outcome": outcome,
                "code": code,
            })
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="bench/results-variance.csv")
    args = parser.parse_args()

    rows = run()
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    total = len(rows)
    quiet = [r for r in rows if r["outcome"] == "wrong, quiet"]
    loud = [r for r in rows if r["outcome"] == "wrong, loud"]
    print(f"{total} edit positions")
    print(f"  agree        {total - len(quiet) - len(loud)}")
    print(f"  wrong, loud  {len(loud)}")
    print(f"  wrong, quiet {len(quiet)}")
    for row in quiet:
        print(f"    {row['edit']} in a {row['position']} -> {row['code']}")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
