from __future__ import annotations

import json

import pytest

from conftest import make_spec
from pexafy_openapi import Document, escape, inline_refs, resolve_pointer, unescape
from pexafy_openapi.errors import BudgetExceeded, LoadError, PointerError, RefError


def test_pointer_escaping_survives_a_round_trip():
    # The two characters RFC 6901 reserves, in the order that catches a
    # replace() done the wrong way round.
    for segment in ("/photos/{id}", "a~1b", "~", "/", "~0", "plain"):
        assert unescape(escape(segment)) == segment


def test_a_path_template_resolves_through_its_escaped_pointer():
    document = Document.from_dict(make_spec())
    op = next(o for o in document.operations() if o.method == "get")
    assert "~1thing~1{thing_id}" in op.ptr
    assert resolve_pointer(document.raw, op.ptr)["operationId"] == "getThing"


def test_pointer_that_does_not_resolve_says_where_it_stopped():
    document = Document.from_dict(make_spec())
    with pytest.raises(PointerError) as exc:
        resolve_pointer(document.raw, "/components/schemas/Missing")
    assert "Missing" in str(exc.value)


def test_external_refs_are_refused_rather_than_fetched():
    spec = make_spec()
    spec["components"]["schemas"]["Thing"] = {"$ref": "https://elsewhere.example/s.json#/T"}
    document = Document.from_dict(spec)
    with pytest.raises(RefError) as exc:
        document.deref(spec["components"]["schemas"]["Thing"], "/components/schemas/Thing")
    assert "bundle" in str(exc.value)


def test_a_ref_pointing_at_itself_raises_instead_of_looping():
    spec = make_spec()
    spec["components"]["schemas"]["Loop"] = {"$ref": "#/components/schemas/Loop"}
    document = Document.from_dict(spec)
    with pytest.raises(RefError):
        document.deref({"$ref": "#/components/schemas/Loop"}, "/x")


def test_operations_follow_a_path_item_ref():
    spec = make_spec()
    spec["paths"]["/aliased"] = {"$ref": "#/components/pathItems/Shared"}
    spec["components"]["pathItems"] = {
        "Shared": {"get": {"operationId": "aliased", "responses": {"200": {"description": "ok"}}}}
    }
    document = Document.from_dict(spec)
    assert "aliased" in {op.operation_id for op in document.operations()}


def test_path_item_parameters_are_inherited_by_every_method():
    from pexafy_openapi.diff import _parameters

    document = Document.from_dict(make_spec())
    for op in document.operations():
        assert ("trace", "header") in _parameters(document, op)


def test_swagger_2_is_rejected_with_a_useful_message():
    with pytest.raises(LoadError) as exc:
        Document.from_dict({"swagger": "2.0", "info": {}})
    assert "Swagger 2.0" in str(exc.value)


def test_json_that_is_not_a_description_is_rejected():
    with pytest.raises(LoadError):
        Document.from_text('{"hello": 1}')


def test_yaml_is_loaded_when_pyyaml_is_available():
    pytest.importorskip("yaml")
    document = Document.from_text(
        "openapi: 3.1.0\ninfo:\n  title: y\n  version: '2.0.0'\npaths: {}\n"
    )
    assert document.version == "2.0.0"


def test_inlining_multiplies_a_shared_component(tmp_path):
    """The whole reason the diff does not inline."""
    spec = make_spec()
    spec["components"]["schemas"]["Wide"] = {
        "type": "object",
        "properties": {f"c{i}": {"$ref": "#/components/schemas/Thing"} for i in range(6)},
    }
    spec["components"]["schemas"]["Wider"] = {
        "type": "object",
        "properties": {f"c{i}": {"$ref": "#/components/schemas/Wide"} for i in range(6)},
    }
    path = tmp_path / "s.json"
    path.write_text(json.dumps(spec))
    document = Document.from_file(path)
    expanded = inline_refs(document)
    # One copy per route to Thing: the component itself, six through Wide,
    # thirty six through Wider, plus the request and the response body.
    assert json.dumps(expanded).count('"alpha"') >= 1 + 6 + 36
    assert len(json.dumps(expanded)) > 5 * len(json.dumps(spec))


def test_inlining_a_recursive_pair_terminates_instead_of_unrolling():
    spec = make_spec()
    spec["components"]["schemas"]["Node"] = {
        "type": "object",
        "properties": {"parent": {"$ref": "#/components/schemas/Node"}},
    }
    spec["paths"]["/thing/{thing_id}"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"] = {"$ref": "#/components/schemas/Node"}
    expanded = inline_refs(Document.from_dict(spec), max_depth=5)
    assert json.dumps(expanded)  # did not recurse forever


def test_inlining_stops_at_the_node_budget_rather_than_the_memory_ceiling():
    spec = make_spec()
    for level in range(8):
        spec["components"]["schemas"][f"L{level}"] = {
            "type": "object",
            "properties": {
                f"c{i}": {"$ref": f"#/components/schemas/L{level + 1}"} for i in range(4)
            },
        }
    spec["components"]["schemas"]["L8"] = {"type": "string"}
    spec["paths"]["/thing/{thing_id}"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"] = {"$ref": "#/components/schemas/L0"}
    with pytest.raises(BudgetExceeded):
        inline_refs(Document.from_dict(spec), node_budget=5_000)


def test_count_nodes_sees_the_whole_document():
    document = Document.from_dict(make_spec())
    assert document.count_nodes() > 50
    assert document.title == "t"
