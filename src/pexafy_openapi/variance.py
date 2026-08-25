"""Which edits break a consumer, and why the answer depends on where you are.

An HTTP API is a function. Requests are its arguments and responses are its
results, so the two sides tolerate change in opposite directions. Anything that
shrinks the set of requests the server accepts breaks callers. Anything that
grows the set of responses the server can return breaks callers. The *same*
edit therefore lands on opposite sides of the line depending on whether the
schema it touched sits under a request body or under a response body:

    add an enum value      safe in a request      breaking in a response
    remove an enum value   breaking in a request  safe in a response
    add a required field   breaking in a request  safe in a response
    remove a field         tolerated in a request breaking in a response
    widen a bound          safe in a request      worth announcing in a response

Two things make this awkward to implement. Variance is not a property of a
schema, it is a property of the route you took to reach it, and in a real
description the same component is reachable both ways: in this API
``SearchFilters`` is read from the query string on ``GET /search/photos`` and
from the JSON body on ``POST /search/photos/by-image``. So variance has to be
carried down the traversal, and a component reached from both sides has to be
judged twice, under both. Annotating components up front does not work.

The second is that the intuition people carry is wrong in one specific place,
and it is the place that bites. Adding a value to an enum feels additive. In a
response it is not. Consumers whose client types came out of a code generator
in Java, Kotlin, C# or Rust deserialise a closed set and raise on anything
outside it, so a value that has never been seen before takes their integration
down at the point they parse the payload, not at the point they use the field.
Nothing in the description changes for them, nothing in their build breaks, and
the failure arrives in production on the day the first row carrying the new
value is indexed. That is the rule this package exists to enforce, and the one
that gets waived deliberately more often than any other, which is why waivers
carry a reason.

The table below is the whole opinion of this package, kept as data so it can be
printed, diffed and argued with. :func:`rule_table` returns it.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Severity(str, Enum):
    """How much a change costs the people already calling the API."""

    BREAKING = "breaking"
    NOTABLE = "notable"
    ADDITIVE = "additive"
    COSMETIC = "cosmetic"

    @property
    def rank(self) -> int:
        return _RANK[self]

    def __ge__(self, other: object) -> bool:  # type: ignore[override]
        if isinstance(other, Severity):
            return self.rank >= other.rank
        return NotImplemented

    def __gt__(self, other: object) -> bool:  # type: ignore[override]
        if isinstance(other, Severity):
            return self.rank > other.rank
        return NotImplemented

    def __le__(self, other: object) -> bool:  # type: ignore[override]
        if isinstance(other, Severity):
            return self.rank <= other.rank
        return NotImplemented

    def __lt__(self, other: object) -> bool:  # type: ignore[override]
        if isinstance(other, Severity):
            return self.rank < other.rank
        return NotImplemented


_RANK = {
    Severity.COSMETIC: 0,
    Severity.ADDITIVE: 1,
    Severity.NOTABLE: 2,
    Severity.BREAKING: 3,
}


class Variance(str, Enum):
    """Which side of the call a schema was reached from."""

    REQUEST = "request"
    RESPONSE = "response"


@dataclass(frozen=True)
class Rule:
    """One kind of edit and what it costs on each side of the call."""

    code: str
    request: Severity
    response: Severity
    why: str

    def severity(self, variance: Variance) -> Severity:
        return self.request if variance is Variance.REQUEST else self.response


def _rule(code: str, request: Severity, response: Severity, why: str) -> Rule:
    return Rule(code, request, response, why)


B, N, A, C = Severity.BREAKING, Severity.NOTABLE, Severity.ADDITIVE, Severity.COSMETIC

#: Every schema-level edit this package knows how to classify.
SCHEMA_RULES: dict[str, Rule] = {
    r.code: r
    for r in [
        _rule(
            "property.added.required", B, A,
            "a field the caller must now send, against a field the caller now also receives",
        ),
        _rule(
            "property.added.optional", A, A,
            "optional on the way in, extra on the way out; consumers that ignore unknown "
            "keys are unaffected either way",
        ),
        _rule(
            "property.removed", N, B,
            "the server stops looking at it, against the server stops sending it; the "
            "second one is a null dereference in every consumer that read the field",
        ),
        _rule(
            "property.removed.closed", B, B,
            "removed from a schema with additionalProperties false, so sending it is now "
            "a 422 rather than an ignored key",
        ),
        _rule(
            "property.became.required", B, A,
            "one more thing to send, against one more thing guaranteed present",
        ),
        _rule(
            "property.became.optional", A, B,
            "the caller may now omit it, against the consumer may no longer rely on it",
        ),
        _rule(
            "enum.value.added", A, B,
            "one more value accepted, against one more value a closed client type has "
            "never heard of",
        ),
        _rule(
            "enum.value.removed", B, A,
            "a value that used to be accepted now 422s, against a value consumers can "
            "stop handling",
        ),
        _rule(
            "enum.introduced", B, N,
            "an open string that is now a closed set rejects requests it used to take",
        ),
        _rule(
            "enum.dropped", A, B,
            "an open string on the way out; consumers that switched exhaustively on the "
            "old set now have an unreachable default that is suddenly reachable",
        ),
        _rule(
            "type.narrowed", B, A,
            "fewer accepted types on the way in, fewer surprises on the way out",
        ),
        _rule(
            "type.widened", A, B,
            "more accepted types on the way in; on the way out a consumer's parser has a "
            "case it does not implement",
        ),
        _rule(
            "type.changed", B, B,
            "a different type on either side is a different contract",
        ),
        _rule(
            "format.changed", N, B,
            "int32 to int64 and date to date-time are silent in the wire format and loud "
            "in the target type a code generator picked",
        ),
        _rule(
            "nullable.added", A, B,
            "null accepted on the way in, against null returned to a consumer whose field "
            "is not optional",
        ),
        _rule(
            "nullable.removed", B, A,
            "callers sending null now fail; consumers stop needing the null branch",
        ),
        _rule(
            "bound.tightened", B, A,
            "maxLength, maximum, maxItems and friends moved inward, so payloads that used "
            "to validate no longer do",
        ),
        _rule(
            "bound.relaxed", A, N,
            "outward is free on the way in; on the way out it means a longer string or a "
            "bigger number than a consumer sized a column for",
        ),
        _rule(
            "additional_properties.closed", B, N,
            "extra keys were tolerated and are now rejected; this is the edit that reads "
            "as tightening validation and lands as a 422 for every caller that sends a "
            "field the description never mentioned",
        ),
        _rule(
            "additional_properties.opened", A, N,
            "extra keys are tolerated again; consumers with strict parsers should be told",
        ),
        _rule(
            "default.changed", N, N,
            "the wire contract is untouched and the behaviour is not; a caller who never "
            "set the parameter gets a different answer than yesterday",
        ),
        _rule(
            "composition.changed", N, N,
            "allOf, oneOf and anyOf are compared positionally; a reordering is reported "
            "so a human looks, not because it is necessarily a break",
        ),
        _rule(
            "discriminator.changed", B, B,
            "consumers dispatch on this field; moving it invalidates every branch",
        ),
        _rule(
            "read_write_only.changed", N, N,
            "readOnly and writeOnly decide which fields a code generator puts on which "
            "model, so flipping one moves fields between types without touching the wire",
        ),
        _rule(
            "description.changed", C, C,
            "prose",
        ),
        _rule(
            "example.changed", C, C,
            "prose",
        ),
    ]
}

#: Edits above the schema level, where variance is fixed by the position.
OPERATION_RULES: dict[str, Rule] = {
    r.code: r
    for r in [
        _rule("path.removed", B, B, "every call to it now 404s"),
        _rule("path.added", A, A, "new surface, nobody was calling it"),
        _rule("operation.removed", B, B, "the method is gone from a path that still exists"),
        _rule("operation.added", A, A, "new method on an existing path"),
        _rule(
            "operation.id.changed", N, N,
            "the wire contract is identical and every generated SDK renames a method, so "
            "consumers who pinned to the SDK get a compile error out of a patch release",
        ),
        _rule("operation.deprecated", N, N, "still works, has a clock on it"),
        _rule("parameter.added.required", B, B, "a query or path parameter callers must now send"),
        _rule("parameter.added.optional", A, A, "opt in, nobody is affected"),
        _rule("parameter.removed", N, N, "the server ignores it; callers still send it harmlessly"),
        _rule("parameter.became.required", B, B, "was optional, is not"),
        _rule("parameter.became.optional", A, A, "was mandatory, is not"),
        _rule(
            "parameter.location.changed", B, B,
            "query to header, or path to query, is a different request",
        ),
        _rule("request_body.became.required", B, B, "an empty POST used to be legal"),
        _rule("request_body.became.optional", A, A, "an empty POST is legal again"),
        _rule("request_body.removed", N, N, "the body is ignored"),
        _rule("content_type.removed", B, B, "callers negotiating that media type get a 415"),
        _rule("content_type.added", A, A, "one more media type accepted or produced"),
        _rule("response.status.removed", B, B, "consumers branching on that status never see it"),
        _rule(
            "response.status.added", A, N,
            "a status consumers have no branch for, which for 4xx and 5xx means their "
            "error handling falls through to the generic case",
        ),
        _rule("response.header.removed", B, B, "consumers reading it get nothing"),
        _rule("response.header.added", A, A, "one more header, ignored by consumers"),
        _rule(
            "security.alternative.added", A, A,
            "the requirement list is a set of alternatives, so one more entry is one more "
            "way to authenticate and nobody is worse off",
        ),
        _rule(
            "security.alternative.removed", B, B,
            "the entry a caller was satisfying is gone, so their credential now gets a 401; "
            "this is the half of a security edit that a diff comparing the list as a "
            "sequence reports as a reorder",
        ),
        _rule("security.scheme.changed", B, B, "the credential itself moved"),
        _rule("security.scheme.removed", B, B, "the scheme a requirement names no longer exists"),
        _rule("server.url.changed", N, N, "clients with a pinned base URL keep using the old one"),
        _rule("info.version.changed", C, C, "the version string itself"),
    ]
}

ALL_RULES: dict[str, Rule] = {**SCHEMA_RULES, **OPERATION_RULES}


def rule_table() -> list[Rule]:
    """Every rule, sorted by code.

    Printed by ``pexafy-openapi rules``. Useful on its own: the table is the
    argument, and the implementation only applies it.
    """
    return sorted(ALL_RULES.values(), key=lambda r: r.code)


def lookup(code: str) -> Rule | None:
    return ALL_RULES.get(code)


def severity_of(code: str, variance: Variance) -> Severity:
    """Severity for a code on one side of the call.

    An unknown code is reported as notable rather than ignored. Silence about a
    change nobody classified is the failure mode that makes people stop trusting
    the tool.
    """
    rule = ALL_RULES.get(code)
    if rule is None:
        return Severity.NOTABLE
    return rule.severity(variance)
