from __future__ import annotations

import pytest

from conftest import make_spec
from pexafy_openapi import Document, Severity, lint, reachability, rule_names
from pexafy_openapi import rules as rules_module


def _findings(spec, rule):
    return list(lint(Document.from_dict(spec), only=[rule]).findings)


def test_a_missing_operation_id_is_a_breaking_finding():
    spec = make_spec()
    spec["paths"]["/thing/{thing_id}"]["get"].pop("operationId")
    findings = _findings(spec, "operation-id-present")
    assert findings and findings[0].severity is Severity.BREAKING


def test_duplicate_operation_ids_are_caught():
    spec = make_spec()
    spec["paths"]["/thing/{thing_id}"]["post"]["operationId"] = "getThing"
    assert _findings(spec, "operation-id-unique")


def test_a_success_response_with_no_schema_is_flagged():
    spec = make_spec()
    spec["paths"]["/thing/{thing_id}"]["get"]["responses"]["200"]["content"][
        "application/json"
    ].pop("schema")
    assert _findings(spec, "success-response-typed")


def test_an_operation_documenting_no_failure_is_flagged():
    spec = make_spec()
    spec["paths"]["/thing/{thing_id}"]["get"]["responses"].pop("404")
    assert _findings(spec, "error-response-documented")


def test_a_closed_enum_is_only_flagged_where_it_is_returned():
    """A closed set on a filter is correct; on a returned field it is a trap."""
    spec = make_spec()
    spec["components"]["schemas"]["Filter"] = {"type": "string", "enum": ["a", "b", "c"]}
    spec["paths"]["/thing/{thing_id}"]["get"]["parameters"].append(
        {"name": "f", "in": "query", "schema": {"$ref": "#/components/schemas/Filter"}}
    )
    flagged = {f.location for f in _findings(spec, "response-enum-extensible")}
    assert "/components/schemas/Thing/properties/kind" in flagged
    assert "/components/schemas/Filter" not in flagged


def test_a_single_value_enum_is_a_discriminant_and_not_flagged():
    spec = make_spec()
    spec["components"]["schemas"]["Thing"]["properties"]["kind"]["enum"] = ["only"]
    assert not _findings(spec, "response-enum-extensible")


def test_an_extensible_marker_silences_the_enum_rule():
    spec = make_spec()
    spec["components"]["schemas"]["Thing"]["properties"]["kind"]["x-extensible-enum"] = True
    assert not _findings(spec, "response-enum-extensible")


def test_a_closed_request_body_is_flagged_as_a_forward_compatibility_trap():
    spec = make_spec()
    spec["components"]["schemas"]["Thing"]["additionalProperties"] = False
    findings = _findings(spec, "request-body-open")
    assert findings and "422" in findings[0].message


def test_a_non_public_server_url_blocks_publication():
    spec = make_spec()
    spec["servers"] = [{"url": "https://staging.example.com/v1"}]
    findings = _findings(spec, "public-server-url")
    assert findings and findings[0].severity is Severity.BREAKING


def test_plain_http_is_notable_rather_than_breaking():
    spec = make_spec()
    spec["servers"] = [{"url": "http://api.example.com/v1"}]
    findings = _findings(spec, "public-server-url")
    assert findings and findings[0].severity is Severity.NOTABLE


def test_mixing_cursor_and_offset_paging_is_flagged():
    spec = make_spec()
    spec["paths"]["/a"] = {
        "get": {
            "operationId": "a",
            "parameters": [{"name": "cursor", "in": "query", "schema": {"type": "string"}}],
            "responses": {"200": {"description": "ok"}},
        }
    }
    spec["paths"]["/b"] = {
        "get": {
            "operationId": "b",
            "parameters": [{"name": "offset", "in": "query", "schema": {"type": "integer"}}],
            "responses": {"200": {"description": "ok"}},
        }
    }
    assert len(_findings(spec, "pagination-consistent")) == 2


def test_an_unreferenced_component_is_reported():
    spec = make_spec()
    spec["components"]["schemas"]["Orphan"] = {"type": "string"}
    locations = {f.location for f in _findings(spec, "schema-reachable")}
    assert "/components/schemas/Orphan" in locations
    assert "/components/schemas/Thing" not in locations


def test_a_component_referenced_only_by_another_component_counts_as_reachable():
    spec = make_spec()
    spec["components"]["schemas"]["Inner"] = {"type": "string"}
    spec["components"]["schemas"]["Thing"]["properties"]["inner"] = {
        "$ref": "#/components/schemas/Inner"
    }
    locations = {f.location for f in _findings(spec, "schema-reachable")}
    assert "/components/schemas/Inner" not in locations


def test_a_rule_that_raises_does_not_take_the_run_down(monkeypatch):
    def explode(document):
        raise RuntimeError("bad node")
        yield  # pragma: no cover

    monkeypatch.setitem(rules_module._REGISTRY, "operation-id-present", explode)
    report = lint(Document.from_dict(make_spec()))
    assert "operation-id-present" in report.crashed
    assert report.to_text()


def test_every_registered_rule_runs_on_a_minimal_document():
    report = lint(Document.from_dict(make_spec()))
    assert report.crashed == []
    assert set(rule_names())


def test_reachability_separates_the_two_sides():
    spec = make_spec()
    spec["components"]["schemas"]["OnlyIn"] = {"type": "string"}
    spec["paths"]["/thing/{thing_id}"]["post"]["requestBody"]["content"]["application/json"][
        "schema"
    ] = {
        "type": "object",
        "properties": {"x": {"$ref": "#/components/schemas/OnlyIn"}},
    }
    result = reachability(Document.from_dict(spec))
    assert "OnlyIn" in result.request_only
    assert "Thing" in result.response_only
    assert result.variances("OnlyIn")


def test_reachability_terminates_on_a_cycle():
    spec = make_spec()
    spec["components"]["schemas"]["A"] = {
        "properties": {"b": {"$ref": "#/components/schemas/B"}}
    }
    spec["components"]["schemas"]["B"] = {
        "properties": {"a": {"$ref": "#/components/schemas/A"}}
    }
    spec["components"]["schemas"]["Thing"]["properties"]["a"] = {
        "$ref": "#/components/schemas/A"
    }
    result = reachability(Document.from_dict(spec))
    assert {"A", "B"} <= result.response_only | result.both


@pytest.mark.parametrize("name", rule_names())
def test_each_rule_is_clean_on_the_published_description(name, spec_dir):
    """The description this repository ships has to pass its own rules."""
    document = Document.from_file(spec_dir / "v1.json")
    report = lint(document, only=[name])
    assert report.crashed == []
    assert report.at_least(Severity.NOTABLE) == []
