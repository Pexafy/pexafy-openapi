"""Differences above the schema level, where variance is fixed by position."""

from __future__ import annotations

from conftest import make_spec
from pexafy_openapi import Document, Severity, diff


def _codes(edit, *, cosmetic=False):
    after = make_spec()
    edit(after)
    report = diff(
        Document.from_dict(make_spec()), Document.from_dict(after), include_cosmetic=cosmetic
    )
    return {c.code: c for c in report.changes}


def test_removing_a_method_from_a_path_that_stays_is_an_operation_removal():
    codes = _codes(lambda s: s["paths"]["/thing/{thing_id}"].pop("post"))
    assert "operation.removed" in codes
    assert "path.removed" not in codes
    assert codes["operation.removed"].severity is Severity.BREAKING


def test_removing_the_last_method_removes_the_path():
    def edit(spec):
        spec["paths"].pop("/thing/{thing_id}")

    codes = _codes(edit)
    assert "path.removed" in codes
    assert "operation.removed" not in codes


def test_a_new_path_is_additive():
    def edit(spec):
        spec["paths"]["/other"] = {
            "get": {"operationId": "other", "responses": {"200": {"description": "ok"}}}
        }

    assert _codes(edit)["path.added"].severity is Severity.ADDITIVE


def test_deprecating_an_operation_is_notable():
    codes = _codes(
        lambda s: s["paths"]["/thing/{thing_id}"]["get"].__setitem__("deprecated", True)
    )
    assert codes["operation.deprecated"].severity is Severity.NOTABLE


def test_making_a_request_body_required_breaks_callers():
    def edit(spec):
        spec["paths"]["/thing/{thing_id}"]["post"]["requestBody"]["required"] = False

    assert _codes(edit)["request_body.became.optional"].severity is Severity.ADDITIVE

    def tighten(spec):
        spec["paths"]["/thing/{thing_id}"]["post"]["requestBody"]["required"] = False

    before = make_spec()
    tighten(before)
    report = diff(Document.from_dict(before), Document.from_dict(make_spec()))
    assert report.breaking[0].code == "request_body.became.required"


def test_dropping_a_media_type_is_breaking():
    def edit(spec):
        content = spec["paths"]["/thing/{thing_id}"]["post"]["requestBody"]["content"]
        content["application/x-www-form-urlencoded"] = content.pop("application/json")

    codes = _codes(edit)
    assert codes["content_type.removed"].severity is Severity.BREAKING
    assert codes["content_type.added"].severity is Severity.ADDITIVE


def test_a_response_header_that_disappears_is_breaking():
    before = make_spec()
    before["paths"]["/thing/{thing_id}"]["get"]["responses"]["200"]["headers"] = {
        "X-Request-Id": {"schema": {"type": "string"}}
    }
    report = diff(Document.from_dict(before), Document.from_dict(make_spec()))
    assert report.breaking[0].code == "response.header.removed"


def test_a_new_error_status_is_notable_for_consumers():
    def edit(spec):
        spec["paths"]["/thing/{thing_id}"]["get"]["responses"]["429"] = {"description": "slow down"}

    assert _codes(edit)["response.status.added"].severity is Severity.NOTABLE


def test_a_server_that_disappears_is_reported():
    def edit(spec):
        spec["servers"] = [{"url": "https://api2.example.com/v1"}]

    assert _codes(edit)["server.url.changed"].severity is Severity.NOTABLE


def test_a_oneof_gaining_a_member_is_notable_rather_than_guessed_at():
    def edit(spec):
        thing = spec["components"]["schemas"]["Thing"]
        thing["oneOf"] = [{"type": "object"}, {"type": "string"}]

    before = make_spec()
    before["components"]["schemas"]["Thing"]["oneOf"] = [{"type": "object"}]
    report = diff(Document.from_dict(before), Document.from_dict(make_spec()))
    codes = {c.code for c in report.changes}
    assert "composition.changed" in codes


def test_moving_the_discriminator_is_breaking():
    def edit(spec):
        spec["components"]["schemas"]["Thing"]["discriminator"] = {"propertyName": "kind"}

    assert _codes(edit)["discriminator.changed"].severity is Severity.BREAKING


def test_flipping_read_only_is_notable_because_it_moves_fields_between_models():
    def edit(spec):
        spec["components"]["schemas"]["Thing"]["properties"]["label"]["readOnly"] = True

    assert _codes(edit)["read_write_only.changed"].severity is Severity.NOTABLE


def test_an_array_item_schema_is_walked():
    before = make_spec()
    before["components"]["schemas"]["Thing"]["properties"]["tags"] = {
        "type": "array",
        "items": {"type": "string", "enum": ["a", "b"]},
    }
    after = make_spec()
    after["components"]["schemas"]["Thing"]["properties"]["tags"] = {
        "type": "array",
        "items": {"type": "string", "enum": ["a"]},
    }
    report = diff(Document.from_dict(before), Document.from_dict(after))
    assert any(c.code == "enum.value.removed" for c in report.changes)


def test_an_unknown_code_would_be_reported_rather_than_dropped():
    from pexafy_openapi import Variance, severity_of

    assert severity_of("something.nobody.classified", Variance.REQUEST) is Severity.NOTABLE


def test_severities_order_the_way_a_threshold_needs_them_to():
    assert Severity.BREAKING > Severity.NOTABLE > Severity.ADDITIVE > Severity.COSMETIC
    assert Severity.ADDITIVE <= Severity.NOTABLE
    assert not Severity.COSMETIC >= Severity.BREAKING
