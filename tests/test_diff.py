"""The classification, exercised through the public entry point.

Every test here edits one document and asserts what comes out for a given side
of the call. Nothing consults the rule table: if the table and the traversal
disagree, these fail.
"""

from __future__ import annotations

import copy

from conftest import make_spec
from pexafy_openapi import Document, Severity, Variance, diff


def _changes(edit, *, code=None, variance=None, cosmetic=False):
    before = make_spec()
    after = make_spec()
    edit(after)
    report = diff(
        Document.from_dict(before), Document.from_dict(after), include_cosmetic=cosmetic
    )
    out = report.changes
    if code:
        out = [c for c in out if c.code == code]
    if variance:
        out = [c for c in out if c.variance is variance]
    return out


def _thing(spec):
    return spec["components"]["schemas"]["Thing"]


def test_the_same_enum_addition_is_breaking_out_and_safe_in():
    """One edit, two verdicts. This is the whole point of the package."""
    changes = _changes(lambda s: _thing(s)["properties"]["kind"]["enum"].append("gamma"),
                       code="enum.value.added")
    by_side = {c.variance: c.severity for c in changes}
    assert by_side[Variance.RESPONSE] is Severity.BREAKING
    assert by_side[Variance.REQUEST] is Severity.ADDITIVE


def test_removing_an_enum_value_flips_the_other_way():
    changes = _changes(lambda s: _thing(s)["properties"]["kind"].__setitem__("enum", ["alpha"]),
                       code="enum.value.removed")
    by_side = {c.variance: c.severity for c in changes}
    assert by_side[Variance.REQUEST] is Severity.BREAKING
    assert by_side[Variance.RESPONSE] is Severity.ADDITIVE


def test_a_new_required_property_breaks_callers_and_not_consumers():
    def edit(spec):
        thing = _thing(spec)
        thing["properties"]["extra"] = {"type": "string"}
        thing["required"] = [*thing["required"], "extra"]

    by_side = {c.variance: c.severity for c in _changes(edit, code="property.added.required")}
    assert by_side[Variance.REQUEST] is Severity.BREAKING
    assert by_side[Variance.RESPONSE] is Severity.ADDITIVE


def test_a_removed_property_breaks_consumers_and_not_callers():
    by_side = {
        c.variance: c.severity
        for c in _changes(lambda s: _thing(s)["properties"].pop("label"),
                          code="property.removed")
    }
    assert by_side[Variance.RESPONSE] is Severity.BREAKING
    assert by_side[Variance.REQUEST] is Severity.NOTABLE


def test_removing_a_property_from_a_closed_object_breaks_callers_too():
    def edit(spec):
        thing = _thing(spec)
        thing["additionalProperties"] = False
        thing["properties"].pop("label")

    request = _changes(edit, code="property.removed.closed", variance=Variance.REQUEST)
    assert request and request[0].severity is Severity.BREAKING


def test_forbidding_unknown_keys_is_breaking_on_the_way_in():
    changes = _changes(lambda s: _thing(s).__setitem__("additionalProperties", False),
                       code="additional_properties.closed")
    by_side = {c.variance: c.severity for c in changes}
    assert by_side[Variance.REQUEST] is Severity.BREAKING
    assert by_side[Variance.RESPONSE] is Severity.NOTABLE


def test_int32_to_int64_is_breaking_on_the_way_out():
    changes = _changes(
        lambda s: _thing(s)["properties"]["count"].__setitem__("format", "int64"),
        code="format.changed",
    )
    by_side = {c.variance: c.severity for c in changes}
    assert by_side[Variance.RESPONSE] is Severity.BREAKING
    assert by_side[Variance.REQUEST] is Severity.NOTABLE


def test_tightening_a_bound_is_breaking_on_the_way_in():
    changes = _changes(
        lambda s: _thing(s)["properties"]["label"].__setitem__("maxLength", 4),
        code="bound.tightened", variance=Variance.REQUEST,
    )
    assert changes and changes[0].severity is Severity.BREAKING


def test_a_pattern_change_is_treated_as_a_tightening():
    """It cannot be shown to be a relaxation without deciding regex containment."""
    changes = _changes(
        lambda s: _thing(s)["properties"]["label"].__setitem__("pattern", "^a.*"),
        code="bound.tightened", variance=Variance.REQUEST,
    )
    assert changes


def test_migrating_from_openapi_30_nullable_to_31_null_is_not_a_change():
    before = make_spec()
    before["openapi"] = "3.0.3"
    before["components"]["schemas"]["Thing"]["properties"]["label"] = {
        "type": "string", "maxLength": 100, "nullable": True,
    }
    after = make_spec()
    after["components"]["schemas"]["Thing"]["properties"]["label"] = {
        "type": ["string", "null"], "maxLength": 100,
    }
    report = diff(Document.from_dict(before), Document.from_dict(after))
    assert [c.code for c in report.changes] == []


def test_a_real_nullability_change_is_still_caught():
    changes = _changes(
        lambda s: _thing(s)["properties"]["label"].__setitem__("type", ["string", "null"]),
        code="nullable.added",
    )
    by_side = {c.variance: c.severity for c in changes}
    assert by_side[Variance.RESPONSE] is Severity.BREAKING
    assert by_side[Variance.REQUEST] is Severity.ADDITIVE


def test_renaming_a_path_placeholder_is_not_a_removed_path():
    before = make_spec()
    after = make_spec()
    after["paths"]["/thing/{id}"] = after["paths"].pop("/thing/{thing_id}")
    report = diff(Document.from_dict(before), Document.from_dict(after))
    assert not [c for c in report.changes if c.code in ("path.removed", "path.added")]


def test_an_operation_id_rename_is_reported_even_though_the_wire_is_identical():
    changes = _changes(
        lambda s: s["paths"]["/thing/{thing_id}"]["get"].__setitem__("operationId", "fetchThing"),
        code="operation.id.changed",
    )
    assert changes and changes[0].severity is Severity.NOTABLE
    assert "SDK" in changes[0].message


def test_a_recursive_schema_terminates_and_is_compared_once():
    before = make_spec()
    before["components"]["schemas"]["Node"] = {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "parent": {"$ref": "#/components/schemas/Node"},
        },
    }
    before["paths"]["/thing/{thing_id}"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"] = {"$ref": "#/components/schemas/Node"}
    after = copy.deepcopy(before)
    after["components"]["schemas"]["Node"]["properties"].pop("name")
    report = diff(Document.from_dict(before), Document.from_dict(after))
    removed = [c for c in report.changes if c.code == "property.removed"]
    assert len(removed) == 1
    assert report.nodes_visited < 40


def test_a_shared_component_is_walked_once_per_side_not_once_per_reference():
    before = make_spec()
    for index in range(20):
        before["paths"][f"/extra{index}"] = {
            "get": {
                "operationId": f"extra{index}",
                "responses": {
                    "200": {
                        "description": "ok",
                        "content": {
                            "application/json": {"schema": {"$ref": "#/components/schemas/Thing"}}
                        },
                    }
                },
            }
        }
    after = copy.deepcopy(before)
    after["components"]["schemas"]["Thing"]["properties"].pop("label")
    report = diff(Document.from_dict(before), Document.from_dict(after))
    assert len([c for c in report.changes if c.code == "property.removed"]) == 2


def test_the_diff_truncates_instead_of_returning_a_list_nobody_reads():
    before = make_spec()
    after = make_spec()
    for index in range(50):
        after["components"]["schemas"]["Thing"]["properties"][f"n{index}"] = {"type": "string"}
    report = diff(Document.from_dict(before), Document.from_dict(after), max_changes=10)
    assert report.truncated
    assert len(report.changes) == 10
    assert report.dropped == 90


def test_a_node_ceiling_reports_truncation_rather_than_raising():
    before = make_spec()
    after = make_spec()
    after["components"]["schemas"]["Thing"]["properties"].pop("label")
    report = diff(Document.from_dict(before), Document.from_dict(after), max_nodes=1)
    assert report.truncated


def test_cosmetic_changes_are_out_of_the_way_unless_asked_for():
    def edit(spec):
        spec["components"]["schemas"]["Thing"]["description"] = "reworded"

    assert _changes(edit) == []
    assert _changes(edit, cosmetic=True)


def test_adding_a_required_query_parameter_breaks_callers():
    def edit(spec):
        spec["paths"]["/thing/{thing_id}"]["get"]["parameters"].append(
            {"name": "mode", "in": "query", "required": True, "schema": {"type": "string"}}
        )

    changes = _changes(edit, code="parameter.added.required")
    assert changes and changes[0].severity is Severity.BREAKING


def test_moving_a_parameter_between_locations_is_not_a_removal():
    def edit(spec):
        for method in ("get", "post"):
            spec["paths"]["/thing/{thing_id}"][method].setdefault("parameters", []).append(
                {"name": "trace", "in": "query", "schema": {"type": "string"}}
            )
        spec["paths"]["/thing/{thing_id}"]["parameters"] = []

    changes = _changes(edit)
    codes = {c.code for c in changes}
    assert "parameter.location.changed" in codes
    assert "parameter.removed" not in codes


def test_dropping_a_documented_status_code_is_breaking():
    changes = _changes(
        lambda s: s["paths"]["/thing/{thing_id}"]["get"]["responses"].pop("404"),
        code="response.status.removed",
    )
    assert changes and changes[0].severity is Severity.BREAKING


def test_narrowing_the_authentication_alternatives_is_breaking():
    """The requirement list is a set of alternatives, so losing one is what breaks callers."""
    changes = _changes(lambda s: s.__setitem__("security", [{"apiKey": [], "mtls": []}]))
    by_code = {c.code: c.severity for c in changes}
    assert by_code["security.alternative.removed"] is Severity.BREAKING
    assert by_code["security.alternative.added"] is Severity.ADDITIVE


def test_a_scheme_that_moves_from_bearer_to_a_header_is_caught():
    """The requirement list is untouched and every caller starts getting a 401."""
    def edit(spec):
        spec["components"]["securitySchemes"]["apiKey"] = {
            "type": "apiKey", "in": "header", "name": "X-Api-Key",
        }

    changes = _changes(edit, code="security.scheme.changed")
    assert changes and changes[0].severity is Severity.BREAKING


def test_a_removed_security_scheme_is_breaking():
    def edit(spec):
        spec["components"]["securitySchemes"].pop("apiKey")

    changes = _changes(edit, code="security.scheme.removed")
    assert changes and changes[0].severity is Severity.BREAKING


def test_the_report_renders_for_a_pull_request_comment():
    changes = _changes(lambda s: _thing(s)["properties"]["kind"]["enum"].append("gamma"))
    assert changes
    report = diff(
        Document.from_dict(make_spec()),
        Document.from_dict(make_spec(**{"info": {"title": "t", "version": "2.0.0"}})),
    )
    markdown = report.to_markdown()
    assert markdown.startswith("### API diff 1.0.0 to 2.0.0")
    assert "No differences." in markdown
    assert report.to_text().startswith("no differences")


def test_the_json_report_round_trips():
    import json

    report = diff(
        Document.from_dict(make_spec()),
        Document.from_dict(make_spec()),
    )
    assert json.loads(report.to_json())["counts"]["breaking"] == 0
