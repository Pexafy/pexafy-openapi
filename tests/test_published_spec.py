"""The description this repository publishes, held to its own rules.

These are the tests that would have caught the mistakes worth catching: a
release snapshot edited in place, a waiver that no longer matches anything, an
enum that grew without anyone deciding to let it.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from pexafy_openapi import Document, Severity, apply_waivers, diff, lint, load_waivers

REPO = Path(__file__).resolve().parent.parent
RELEASES = sorted((REPO / "openapi" / "releases").glob("*.json"))


def test_the_current_description_is_the_newest_release():
    current = Document.from_file(REPO / "openapi" / "v1.json")
    newest = max(RELEASES, key=lambda p: [int(n) for n in p.stem.split(".")])
    assert current.version == newest.stem
    assert json.loads(newest.read_text()) == current.raw


@pytest.mark.parametrize("path", RELEASES, ids=lambda p: p.stem)
def test_every_release_snapshot_loads_and_declares_its_own_version(path):
    document = Document.from_file(path)
    assert document.version == path.stem
    assert list(document.operations())


def test_the_published_description_lints_clean_above_cosmetic():
    report = lint(Document.from_file(REPO / "openapi" / "v1.json"))
    assert report.crashed == []
    assert report.at_least(Severity.NOTABLE) == []


def test_the_only_break_since_the_last_release_is_the_one_that_was_waived():
    report = diff(
        Document.from_file(REPO / "openapi" / "releases" / "1.2.0.json"),
        Document.from_file(REPO / "openapi" / "v1.json"),
    )
    result = apply_waivers(
        report, load_waivers(REPO / "waivers.json"), today=date(2026, 9, 1)
    )
    assert result.remaining == []
    assert [c.code for c, _ in result.waived] == ["enum.value.added"]


def test_the_waived_change_is_the_asymmetric_one():
    """Source gained a value: breaking where it is returned, additive where it is sent."""
    report = diff(
        Document.from_file(REPO / "openapi" / "releases" / "1.2.0.json"),
        Document.from_file(REPO / "openapi" / "v1.json"),
    )
    sides = {
        c.variance.value: c.severity
        for c in report.changes
        if c.code == "enum.value.added" and c.location == "/components/schemas/Source"
    }
    assert sides == {"response": Severity.BREAKING, "request": Severity.ADDITIVE}


def test_the_previous_release_was_purely_additive():
    report = diff(
        Document.from_file(REPO / "openapi" / "releases" / "1.1.0.json"),
        Document.from_file(REPO / "openapi" / "releases" / "1.2.0.json"),
    )
    assert report.at_least(Severity.NOTABLE) == []
    assert report.changes


def test_every_waiver_in_the_repository_still_matches_something():
    """An unused waiver is a lie about what was decided; delete it."""
    report = diff(
        Document.from_file(REPO / "openapi" / "releases" / "1.2.0.json"),
        Document.from_file(REPO / "openapi" / "v1.json"),
    )
    result = apply_waivers(
        report, load_waivers(REPO / "waivers.json"), today=date(2026, 9, 1)
    )
    assert result.unused == []


def test_the_benchmark_results_are_committed_and_have_rows():
    for name in ("results-scaling.csv", "results-variance.csv"):
        lines = (REPO / "bench" / name).read_text().strip().splitlines()
        assert len(lines) > 5, name
