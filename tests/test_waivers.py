from __future__ import annotations

import json
from datetime import date

import pytest

from conftest import make_spec
from pexafy_openapi import Document, Severity, apply_waivers, diff, load_waivers, parse_waivers
from pexafy_openapi.errors import LoadError


def _breaking_report():
    before = make_spec()
    after = make_spec()
    after["components"]["schemas"]["Thing"]["properties"]["kind"]["enum"].append("gamma")
    return diff(Document.from_dict(before), Document.from_dict(after))


def test_a_waiver_without_a_reason_is_refused():
    """A waiver with no reason is a mute button, which is what this file exists to avoid."""
    with pytest.raises(LoadError) as exc:
        parse_waivers({"waivers": [{"code": "enum.value.added"}]})
    assert "reason" in str(exc.value)


def test_a_waiver_with_an_unparseable_date_is_refused():
    with pytest.raises(LoadError):
        parse_waivers({"waivers": [{"code": "x", "reason": "y", "expires": "next tuesday"}]})


def test_a_matching_waiver_absorbs_the_breaking_change():
    report = _breaking_report()
    waivers = parse_waivers([
        {"code": "enum.value.added", "location": "/components/schemas/*",
         "reason": "announced in the changelog"}
    ])
    result = apply_waivers(report, waivers, today=date(2026, 1, 1))
    assert result.remaining == []
    assert len(result.waived) == 1
    assert result.waived[0][1].reason.startswith("announced")


def test_a_waiver_scoped_to_another_location_does_not_apply():
    report = _breaking_report()
    waivers = parse_waivers([
        {"code": "enum.value.added", "location": "/paths/*", "reason": "elsewhere"}
    ])
    result = apply_waivers(report, waivers, today=date(2026, 1, 1))
    assert result.remaining
    assert result.unused == waivers


def test_an_expired_waiver_stops_covering_and_is_reported():
    report = _breaking_report()
    waivers = parse_waivers([
        {"code": "enum.value.added", "reason": "was fine once", "expires": "2020-01-01"}
    ])
    result = apply_waivers(report, waivers, today=date(2026, 1, 1))
    assert result.expired == waivers
    assert result.remaining, "an expired waiver must not keep absorbing the change"


def test_only_changes_at_or_above_the_threshold_can_be_waived():
    report = _breaking_report()
    waivers = parse_waivers([{"code": "*", "reason": "everything"}])
    result = apply_waivers(report, waivers, threshold=Severity.BREAKING, today=date(2026, 1, 1))
    codes = {c.code for c, _ in result.waived}
    assert codes == {"enum.value.added"}
    assert all(c.severity is Severity.BREAKING for c, _ in result.waived)


def test_waivers_load_from_a_file(tmp_path):
    path = tmp_path / "waivers.json"
    path.write_text(json.dumps({"waivers": [{"code": "a.b", "reason": "because"}]}))
    waivers = load_waivers(path)
    assert waivers[0].code == "a.b"
    assert waivers[0].to_dict()["expires"] is None


def test_a_missing_waiver_file_is_a_load_error(tmp_path):
    with pytest.raises(LoadError):
        load_waivers(tmp_path / "nope.json")


def test_the_result_serialises():
    report = _breaking_report()
    waivers = parse_waivers([{"code": "enum.value.added", "reason": "ok"}])
    payload = apply_waivers(report, waivers, today=date(2026, 1, 1)).to_dict()
    assert payload["waived"][0]["waiver"]["reason"] == "ok"
