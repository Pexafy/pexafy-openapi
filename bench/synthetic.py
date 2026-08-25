"""Descriptions built to a shape, so the benchmarks measure one thing at a time.

The parameter that matters is ``depth``. Real OpenAPI documents are shallow in
the file and deep once you follow the ``$ref``s: an envelope holds a list of
resources, each resource holds three sub-objects, each of those holds an enum
component. Four levels of nesting with a fan-out of three is unremarkable and
it is already 81 leaf copies if you expand it.

``chain`` builds exactly that shape and nothing else, so the cost curve is not
confounded by how many operations a document happens to have. ``realistic``
mixes in the parts that make a real document awkward — a recursive pair, a
component referenced from twenty places, a oneOf — and is used to sanity check
that the numbers from ``chain`` are not an artefact of the shape.
"""

from __future__ import annotations

from typing import Any


def _ref(name: str) -> dict[str, Any]:
    return {"$ref": f"#/components/schemas/{name}"}


def chain(depth: int, fan: int = 3, operations: int = 4) -> dict[str, Any]:
    """A ladder of components, each level referencing the next ``fan`` times."""
    schemas: dict[str, Any] = {}
    for level in range(depth):
        if level == depth - 1:
            schemas[f"L{level}"] = {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "format": "uuid"},
                    "kind": {"type": "string", "enum": ["a", "b", "c"]},
                    "size": {"type": "integer", "minimum": 0, "maximum": 1000},
                },
            }
        else:
            schemas[f"L{level}"] = {
                "type": "object",
                "required": [f"child{index}" for index in range(fan)],
                "properties": {
                    f"child{index}": _ref(f"L{level + 1}") for index in range(fan)
                },
            }

    paths: dict[str, Any] = {}
    for index in range(operations):
        paths[f"/resource{index}"] = {
            "get": {
                "operationId": f"getResource{index}",
                "parameters": [
                    {
                        "name": "filter",
                        "in": "query",
                        "schema": {"type": "string", "enum": ["one", "two"]},
                    }
                ],
                "responses": {
                    "200": {
                        "description": "ok",
                        "content": {"application/json": {"schema": _ref("L0")}},
                    },
                    "404": {"description": "gone"},
                },
            },
            "post": {
                "operationId": f"putResource{index}",
                "requestBody": {
                    "required": True,
                    "content": {"application/json": {"schema": _ref("L0")}},
                },
                "responses": {
                    "201": {
                        "description": "made",
                        "content": {"application/json": {"schema": _ref("L0")}},
                    },
                    "422": {"description": "no"},
                },
            },
        }

    return {
        "openapi": "3.1.0",
        "info": {"title": "synthetic", "version": "1.0.0"},
        "servers": [{"url": "https://api.example.com/v1"}],
        "paths": paths,
        "components": {"schemas": schemas},
    }


def realistic(depth: int, fan: int = 3, operations: int = 20) -> dict[str, Any]:
    """``chain`` plus the two shapes that break naive traversal.

    A recursive pair has no finite expansion at all, and a hub component
    referenced from every operation is what makes memoisation pay.
    """
    document = chain(depth, fan, operations)
    schemas = document["components"]["schemas"]
    schemas["Node"] = {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "parent": _ref("Tree"),
            "tags": {"type": "array", "items": {"type": "string"}},
        },
    }
    schemas["Tree"] = {
        "type": "object",
        "properties": {
            "root": _ref("Node"),
            "children": {"type": "array", "items": _ref("Node")},
        },
    }
    schemas["Hub"] = {
        "oneOf": [_ref("L0"), _ref("Tree")],
        "discriminator": {"propertyName": "kind"},
    }
    for path in document["paths"].values():
        path["get"]["responses"]["200"]["content"]["application/json"]["schema"] = _ref("Hub")
    return document


def edited(document: dict[str, Any], *, add_enum_value: bool = True) -> dict[str, Any]:
    """One realistic edit, so the diff has something to find at every depth."""
    import copy

    after = copy.deepcopy(document)
    # Sort the level names numerically. Sorting them as strings puts L9 after
    # L10 and the edit lands on a branch node, which silently produces a diff
    # with nothing in it at depth 11 and above.
    levels = [k for k in after["components"]["schemas"] if k[1:].isdigit()]
    leaf = max(levels, key=lambda k: int(k[1:]))
    kind = after["components"]["schemas"][leaf]["properties"].get("kind")
    if kind and add_enum_value:
        kind["enum"] = [*kind["enum"], "d"]
    after["info"]["version"] = "1.1.0"
    return after
