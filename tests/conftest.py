from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parent.parent
SPEC_DIR = REPO / "openapi"


def _schema() -> dict[str, Any]:
    return {
        "type": "object",
        "required": ["kind"],
        "properties": {
            "kind": {"type": "string", "enum": ["alpha", "beta"]},
            "label": {"type": "string", "maxLength": 100},
            "count": {"type": "integer", "format": "int32"},
        },
    }


def make_spec(**overrides: Any) -> dict[str, Any]:
    """A document with the same component on both sides of one call.

    Every variance test needs a request route and a response route to the same
    schema; building it once here keeps the tests about the classification.
    """
    document: dict[str, Any] = {
        "openapi": "3.1.0",
        "info": {"title": "t", "version": "1.0.0"},
        "servers": [{"url": "https://api.example.com/v1"}],
        "security": [{"apiKey": []}],
        "paths": {
            "/thing/{thing_id}": {
                "parameters": [
                    {
                        "name": "trace",
                        "in": "header",
                        "schema": {"type": "string"},
                    }
                ],
                "get": {
                    "operationId": "getThing",
                    "parameters": [
                        {
                            "name": "thing_id",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string"},
                        }
                    ],
                    "responses": {
                        "200": {
                            "description": "ok",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/Thing"},
                                    "example": {"kind": "alpha"},
                                }
                            },
                        },
                        "404": {"description": "gone"},
                    },
                },
                "post": {
                    "operationId": "putThing",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/Thing"}
                            }
                        },
                    },
                    "responses": {
                        "201": {
                            "description": "made",
                            "content": {
                                "application/json": {
                                    "schema": {"type": "object"},
                                    "example": {},
                                }
                            },
                        },
                        "422": {"description": "no"},
                    },
                },
            }
        },
        "components": {
            "securitySchemes": {"apiKey": {"type": "http", "scheme": "bearer"}},
            "schemas": {"Thing": _schema()},
        },
    }
    document.update(copy.deepcopy(overrides))
    return document


@pytest.fixture
def spec() -> dict[str, Any]:
    return make_spec()


@pytest.fixture
def spec_dir() -> Path:
    return SPEC_DIR
