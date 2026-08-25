"""Cost of diffing two descriptions, expanded against not expanded.

The question this answers is whether it is worth not taking the obvious route.
The obvious route is to resolve every ``$ref`` into a self-contained tree and
then compare two plain dictionaries, which is a hundred lines and is what most
tooling does. It works until the document nests, and then it stops working
quite suddenly.

Run it:

    python bench/bench_diff.py --out bench/results-scaling.csv

Each row is one document shape. ``inline_nodes`` is how many nodes the expanded
tree holds against ``source_nodes`` in the file, and the ratio is the whole
argument. Timings come from the best of three runs on an otherwise idle
machine; the shape of the curve is what matters, not the constant, and the
constant will not reproduce on your laptop.
"""

from __future__ import annotations

import argparse
import csv
import gc
import sys
import time
import tracemalloc
from collections.abc import Callable
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from synthetic import chain, edited, realistic  # noqa: E402

from pexafy_openapi import Document, diff, inline_refs  # noqa: E402
from pexafy_openapi.errors import BudgetExceeded  # noqa: E402

FIELDS = [
    "shape",
    "depth",
    "source_nodes",
    "inline_nodes",
    "blowup",
    "inline_ms",
    "inline_peak_mb",
    "diff_ms",
    "diff_peak_mb",
    "diff_nodes_visited",
    "changes",
]


def _count(node: Any) -> int:
    total = 0
    stack = [node]
    while stack:
        current = stack.pop()
        total += 1
        if isinstance(current, dict):
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)
    return total


def _timed(func: Callable[[], Any], repeats: int = 3) -> tuple[float, float, Any]:
    """Best wall time of ``repeats`` runs, plus peak allocation of one of them."""
    gc.collect()
    best = float("inf")
    result = None
    for _ in range(repeats):
        start = time.perf_counter()
        result = func()
        best = min(best, time.perf_counter() - start)
    gc.collect()
    tracemalloc.start()
    func()
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    return best * 1000, peak / 1_048_576, result


def measure(shape: str, before: dict, after: dict, depth: int, budget: int) -> dict[str, Any]:
    old = Document.from_dict(before)
    new = Document.from_dict(after)

    row: dict[str, Any] = {
        "shape": shape,
        "depth": depth,
        "source_nodes": old.count_nodes(),
        "inline_nodes": "",
        "blowup": "",
        "inline_ms": "",
        "inline_peak_mb": "",
    }

    try:
        inline_ms, inline_peak, expanded = _timed(
            lambda: inline_refs(old, max_depth=40, node_budget=budget), repeats=1
        )
    except BudgetExceeded:
        row["inline_nodes"] = f">{budget}"
        row["blowup"] = f">{budget / row['source_nodes']:.0f}"
    else:
        nodes = _count(expanded)
        row["inline_nodes"] = nodes
        row["blowup"] = round(nodes / row["source_nodes"], 1)
        row["inline_ms"] = round(inline_ms, 2)
        row["inline_peak_mb"] = round(inline_peak, 2)

    diff_ms, diff_peak, report = _timed(lambda: diff(old, new))
    row["diff_ms"] = round(diff_ms, 2)
    row["diff_peak_mb"] = round(diff_peak, 3)
    row["diff_nodes_visited"] = report.nodes_visited
    row["changes"] = len(report.changes)
    return row


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="bench/results-scaling.csv")
    parser.add_argument("--max-depth", type=int, default=11)
    parser.add_argument("--fan", type=int, default=3)
    parser.add_argument(
        "--budget", type=int, default=4_000_000,
        help="node ceiling for the inlining run; past this it is called a failure",
    )
    parser.add_argument("--spec", action="append", default=[],
                        help="also measure a real document (repeatable)")
    args = parser.parse_args()

    rows: list[dict[str, Any]] = []
    for depth in range(2, args.max_depth + 1):
        before = chain(depth, args.fan)
        rows.append(measure("chain", before, edited(before), depth, args.budget))
        print(f"chain depth={depth} {rows[-1]['blowup']}x", file=sys.stderr)

    for depth in (4, 6, 8):
        before = realistic(depth, args.fan)
        rows.append(measure("realistic", before, edited(before), depth, args.budget))
        print(f"realistic depth={depth} {rows[-1]['blowup']}x", file=sys.stderr)

    for path in args.spec:
        import json

        payload = json.loads(Path(path).read_text())
        rows.append(measure(Path(path).name, payload, payload, 0, args.budget))
        print(f"{path} {rows[-1]['blowup']}x", file=sys.stderr)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    print(f"wrote {out} ({len(rows)} rows)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
