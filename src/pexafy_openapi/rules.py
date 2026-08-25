"""Checks on a single description, aimed at the things that hurt later.

These are not style rules. Each one exists because of a specific way a
published description costs somebody time:

* An operation without an ``operationId``, or with one that keeps moving, makes
  every code generator invent a method name from the path. Two releases later
  the invented name changes because a path gained a segment, and consumers who
  pinned to a generated SDK get a compile error out of what the changelog calls
  a documentation fix.
* A 2xx response with no schema means the generator produces a method returning
  an untyped blob. Nobody notices until they try to use the SDK.
* A closed enum in a response with nothing saying it can grow is the single
  most expensive line in a public description. See
  :mod:`pexafy_openapi.variance`.
* A server URL pointing at something that is not the public API is how staging
  hosts end up in documentation portals. It has happened to enough teams to be
  worth one regular expression.

Every rule runs inside its own guard. A rule that raises produces a finding
against itself and the rest of the run continues, because a linter that aborts
on the first malformed node is a linter that cannot be pointed at somebody
else's description, which is most of the reason to have one.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

from .document import Document, join, pointer
from .reach import reachability
from .variance import Severity

#: Hosts that should never appear in a description that ships to a docs portal.
NON_PUBLIC_HOST = re.compile(
    r"(?i)\b(?:localhost|127\.0\.0\.1|0\.0\.0\.0|\S*\.(?:local|test|internal|lan)\b"
    r"|staging\.\S+|preprod\.\S+|dev\.\S+)"
)


@dataclass(frozen=True)
class Finding:
    rule: str
    severity: Severity
    location: str
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule": self.rule,
            "severity": self.severity.value,
            "location": self.location,
            "message": self.message,
        }

    def __str__(self) -> str:
        return f"{self.severity.value:9} {self.rule}\n    {self.location}\n    {self.message}"


@dataclass
class LintReport:
    findings: list[Finding] = field(default_factory=list)
    #: Rules that raised. Kept apart from findings so a broken rule cannot be
    #: mistaken for a clean run.
    crashed: list[str] = field(default_factory=list)

    def at_least(self, severity: Severity) -> list[Finding]:
        return [f for f in self.findings if f.severity >= severity]

    def counts(self) -> dict[str, int]:
        out = {s.value: 0 for s in Severity}
        for finding in self.findings:
            out[finding.severity.value] += 1
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "counts": self.counts(),
            "crashed_rules": self.crashed,
            "findings": [f.to_dict() for f in self.findings],
        }

    def to_text(self) -> str:
        if not self.findings and not self.crashed:
            return "clean"
        lines = [str(f) for f in self.findings]
        for name in self.crashed:
            lines.append(f"error     {name}\n    the rule itself raised; findings may be missing")
        counts = self.counts()
        lines += ["", "  ".join(f"{counts[s.value]} {s.value}" for s in Severity)]
        return "\n".join(lines)


Rule = Callable[[Document], Iterator[Finding]]
_REGISTRY: dict[str, Rule] = {}


def rule(name: str) -> Callable[[Rule], Rule]:
    def register(func: Rule) -> Rule:
        _REGISTRY[name] = func
        func.rule_name = name  # type: ignore[attr-defined]
        return func

    return register


# --------------------------------------------------------------------------


@rule("operation-id-present")
def _operation_id_present(document: Document) -> Iterator[Finding]:
    for op in document.operations():
        if not op.operation_id:
            yield Finding(
                "operation-id-present", Severity.BREAKING, op.ptr,
                f"{op} has no operationId, so every generator invents a method name "
                "from the path and renames it the day the path changes",
            )


@rule("operation-id-unique")
def _operation_id_unique(document: Document) -> Iterator[Finding]:
    seen: dict[str, str] = {}
    for op in document.operations():
        oid = op.operation_id
        if not oid:
            continue
        if oid in seen:
            yield Finding(
                "operation-id-unique", Severity.BREAKING, op.ptr,
                f"operationId {oid!r} is used by both {seen[oid]} and {op}; "
                "generators either overwrite one method with the other or refuse to run",
            )
        else:
            seen[oid] = str(op)


@rule("success-response-typed")
def _success_response_typed(document: Document) -> Iterator[Finding]:
    for op in document.operations():
        responses = op.body.get("responses") or {}
        if not isinstance(responses, dict):
            continue
        for status, response in responses.items():
            if not status.startswith("2") or status == "204":
                continue
            _, resolved = document.deref(response, join(op.ptr, "responses", status))
            if not isinstance(resolved, dict):
                continue
            content = resolved.get("content")
            if not content:
                yield Finding(
                    "success-response-typed", Severity.NOTABLE,
                    join(op.ptr, "responses", status),
                    f"{op} returns {status} with no content; if that is deliberate it "
                    "should be a 204",
                )
                continue
            for media, body in content.items():
                if isinstance(body, dict) and "schema" not in body:
                    yield Finding(
                        "success-response-typed", Severity.NOTABLE,
                        join(op.ptr, "responses", status, "content", media),
                        f"{op} {status} {media} has no schema, so consumers get an "
                        "untyped payload",
                    )


@rule("error-response-documented")
def _error_response_documented(document: Document) -> Iterator[Finding]:
    """Every operation has to say what its failures look like.

    Not for completeness. An operation that documents only its 200 makes
    generators produce a client whose error path is whatever the HTTP library
    does by default, and the consumer discovers the shape of an error the first
    time they get one, in production.
    """
    for op in document.operations():
        responses = op.body.get("responses") or {}
        if not isinstance(responses, dict):
            continue
        codes = [str(c) for c in responses]
        if not any(c.startswith(("4", "5")) or c == "default" for c in codes):
            yield Finding(
                "error-response-documented", Severity.NOTABLE, join(op.ptr, "responses"),
                f"{op} documents no failure response",
            )


@rule("response-enum-extensible")
def _response_enum_extensible(document: Document) -> Iterator[Finding]:
    """Closed enums *in responses* must carry ``x-extensible-enum`` or a note.

    The vendor extension is the only thing that lets a consumer's generator emit
    an open type. Without it, adding one value to the list is a breaking change
    forever, which in practice means the list never grows and somebody ends up
    passing the real value in a second field.

    Restricted to components reachable from response position. Applying it to
    every enum in the document flags request filters, where a closed set is
    correct and where the rule would be pure noise. Working that out needs
    :func:`~pexafy_openapi.reach.reachability`, not a naming convention.
    """
    reach = reachability(document)
    returned = reach.response_only | reach.both
    for name, schema in document.schemas().items():
        if name not in returned:
            continue
        base = pointer("components", "schemas", name)
        for ptr, node in _walk_schemas(schema, base):
            values = node.get("enum")
            if not isinstance(values, list):
                continue
            # A one-value enum is a discriminant, not a vocabulary: `success:
            # false` on an error envelope is the shape of the branch and can
            # never grow. Booleans are closed by definition. Flagging either
            # produces a finding nobody can act on, and one of those per run is
            # enough for people to stop reading the output.
            if len(values) < 2 or all(isinstance(v, bool) for v in values):
                continue
            if node.get("x-extensible-enum") or "extensible" in str(node.get("description", "")):
                continue
            yield Finding(
                "response-enum-extensible", Severity.NOTABLE, ptr,
                f"closed enum of {len(node['enum'])} values with nothing saying it can "
                "grow; adding a value later is a breaking change for consumers whose "
                "client types came out of a generator",
            )


@rule("component-variance-conflict")
def _component_variance_conflict(document: Document) -> Iterator[Finding]:
    """Components reached from both sides of the call.

    Not a defect. A warning that an edit here is judged twice under opposite
    rules, so the reviewer who approved it looking at one operation has seen
    half the consequence. Splitting the component into an input and an output
    variant is usually cheaper than arguing about it every release.
    """
    reach = reachability(document)
    for name in sorted(reach.both):
        yield Finding(
            "component-variance-conflict", Severity.COSMETIC,
            pointer("components", "schemas", name),
            f"{name} is read from requests and returned in responses; adding a required "
            "field to it is additive on one side and breaking on the other",
        )


@rule("request-body-open")
def _request_body_open(document: Document) -> Iterator[Finding]:
    """``additionalProperties: false`` on a request body is a forward-compat trap."""
    for op in document.operations():
        body = op.body.get("requestBody")
        if body is None:
            continue
        _, resolved = document.deref(body, join(op.ptr, "requestBody"))
        if not isinstance(resolved, dict):
            continue
        for media, entry in (resolved.get("content") or {}).items():
            if not isinstance(entry, dict) or "schema" not in entry:
                continue
            base = join(op.ptr, "requestBody", "content", media, "schema")
            _, schema = document.deref(entry["schema"], base)
            for ptr, node in _walk_schemas(schema, base):
                if node.get("additionalProperties") is False:
                    yield Finding(
                        "request-body-open", Severity.NOTABLE, ptr,
                        f"{op} rejects unknown keys here; an older client that keeps "
                        "sending a field you removed gets a 422 instead of being ignored",
                    )


@rule("pagination-consistent")
def _pagination_consistent(document: Document) -> Iterator[Finding]:
    """Collection endpoints must page the same way as each other.

    Mixing cursor paging on one list endpoint with offset paging on the next is
    the thing consumers write the most wrapper code around, and the thing they
    complain about first.
    """
    styles: dict[str, list[str]] = {}
    for op in document.operations():
        if op.method != "get":
            continue
        names = {
            p.get("name")
            for p in (op.body.get("parameters") or [])
            if isinstance(p, dict)
        }
        if "cursor" in names:
            styles.setdefault("cursor", []).append(str(op))
        elif {"offset", "page"} & names:
            styles.setdefault("offset", []).append(str(op))
    if len(styles) > 1:
        for style, ops in sorted(styles.items()):
            yield Finding(
                "pagination-consistent", Severity.NOTABLE, "/paths",
                f"{style} paging on {', '.join(ops[:3])}"
                + (f" and {len(ops) - 3} more" if len(ops) > 3 else ""),
            )


@rule("public-server-url")
def _public_server_url(document: Document) -> Iterator[Finding]:
    for index, server in enumerate(document.raw.get("servers") or []):
        if not isinstance(server, dict):
            continue
        url = str(server.get("url", ""))
        if NON_PUBLIC_HOST.search(url):
            yield Finding(
                "public-server-url", Severity.BREAKING, pointer("servers", index),
                f"server {index} points somewhere that is not the public API; "
                "this is a published document",
            )
        elif url.startswith("http://"):
            yield Finding(
                "public-server-url", Severity.NOTABLE, pointer("servers", index),
                f"server {index} is plain HTTP",
            )


@rule("security-declared")
def _security_declared(document: Document) -> Iterator[Finding]:
    global_security = document.raw.get("security")
    if global_security:
        return
    for op in document.operations():
        if "security" not in op.body:
            yield Finding(
                "security-declared", Severity.NOTABLE, op.ptr,
                f"{op} inherits no security requirement and declares none, so the "
                "description says it is public",
            )


@rule("schema-reachable")
def _schema_reachable(document: Document) -> Iterator[Finding]:
    """Components nothing points at.

    Harmless in the document and not harmless downstream: generators emit a
    model class for every component, so an abandoned schema becomes a public
    type in somebody's SDK that they then have to keep.
    """
    referenced: set[str] = set()

    def collect(node: Any) -> None:
        stack = [node]
        while stack:
            current = stack.pop()
            if isinstance(current, dict):
                ref = current.get("$ref")
                if isinstance(ref, str):
                    referenced.add(ref.split("/")[-1])
                stack.extend(current.values())
            elif isinstance(current, list):
                stack.extend(current)

    collect(document.raw.get("paths"))
    # A component referenced only by another component is still only reachable
    # if that one is, so walk to a fixed point rather than one level.
    schemas = document.schemas()
    frontier = set(referenced)
    while frontier:
        nxt: set[str] = set()
        for name in frontier:
            if name in schemas:
                before = set(referenced)
                collect(schemas[name])
                nxt |= referenced - before
        frontier = nxt
    for name in sorted(set(schemas) - referenced):
        yield Finding(
            "schema-reachable", Severity.COSMETIC, pointer("components", "schemas", name),
            f"{name} is not referenced from any operation",
        )


@rule("example-present")
def _example_present(document: Document) -> Iterator[Finding]:
    for op in document.operations():
        responses = op.body.get("responses") or {}
        if not isinstance(responses, dict):
            continue
        for status, response in responses.items():
            if not status.startswith("2"):
                continue
            _, resolved = document.deref(response, join(op.ptr, "responses", status))
            if not isinstance(resolved, dict):
                continue
            for media, entry in (resolved.get("content") or {}).items():
                if not isinstance(entry, dict):
                    continue
                schema = entry.get("schema")
                _, schema = document.deref(schema, "") if schema else ("", {})
                has = any(k in entry for k in ("example", "examples")) or (
                    isinstance(schema, dict) and "example" in schema
                )
                if not has:
                    yield Finding(
                        "example-present", Severity.COSMETIC,
                        join(op.ptr, "responses", status, "content", media),
                        f"{op} {status} has no example, so the docs portal renders the "
                        "schema and nothing a reader can paste",
                    )


def _walk_schemas(node: Any, ptr: str, depth: int = 0) -> Iterator[tuple[str, dict]]:
    """Every schema object under a node, without following ``$ref``.

    Not following refs is deliberate: components are walked from the top by the
    caller, so following would visit each one once per reference and report the
    same finding several times.
    """
    if depth > 40 or not isinstance(node, dict) or "$ref" in node:
        return
    yield ptr, node
    for key in ("properties", "patternProperties"):
        for name, child in (node.get(key) or {}).items():
            yield from _walk_schemas(child, join(ptr, key, name), depth + 1)
    for key in ("allOf", "oneOf", "anyOf", "prefixItems"):
        for index, child in enumerate(node.get(key) or []):
            yield from _walk_schemas(child, join(ptr, key, index), depth + 1)
    for key in ("items", "additionalProperties", "not"):
        child = node.get(key)
        if isinstance(child, dict):
            yield from _walk_schemas(child, join(ptr, key), depth + 1)


def rule_names() -> list[str]:
    return sorted(_REGISTRY)


def lint(
    document: Document,
    *,
    only: list[str] | None = None,
    skip: list[str] | None = None,
) -> LintReport:
    """Run the rules and collect what they found.

    Each rule is isolated. A rule that raises is recorded in ``crashed`` and the
    run continues, so pointing this at a description that breaks an assumption
    still returns the findings from every other rule.
    """
    report = LintReport()
    for name in rule_names():
        if only and name not in only:
            continue
        if skip and name in skip:
            continue
        try:
            report.findings.extend(_REGISTRY[name](document))
        except Exception:  # noqa: BLE001 - reported, not swallowed
            report.crashed.append(name)
    report.findings.sort(key=lambda f: (-f.severity.rank, f.location, f.rule))
    return report
