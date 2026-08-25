"""Which components are read on the way in, on the way out, or both.

This is the piece that makes variance usable in practice. A schema does not
have a variance; a *route to* a schema does. Ask which components are reachable
from request position and which from response position and you get three
groups, and the third one is where the arguments happen.

In this API ``SearchFilters`` is in that third group: the same component backs
the query string of ``GET /search/photos`` and the JSON body of
``POST /search/photos/by-image``. Anything edited there has to satisfy both
sides at once, which is usually fine because both sides are requests. The
expensive case is a component used as a request body on one operation and
returned as the created resource on another, which is the normal shape of a
create endpoint. Adding a required field to it is additive for the response and
breaking for the request, and a reviewer looking at one operation sees only
half of that.

Reachability is computed with an explicit worklist over ``(component,
variance)`` pairs rather than by recursion. Component graphs are cyclic —
``Collection`` holds ``Photo`` which links back to ``Collection`` — so a
recursive walk either needs a visited set anyway or does not terminate, and the
worklist makes the visited set the whole state.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from .document import Document, join
from .variance import Variance


@dataclass
class Reachability:
    """Component names grouped by the side of the call they are reached from."""

    request_only: set[str] = field(default_factory=set)
    response_only: set[str] = field(default_factory=set)
    both: set[str] = field(default_factory=set)
    unreachable: set[str] = field(default_factory=set)

    def variances(self, name: str) -> set[Variance]:
        if name in self.both:
            return {Variance.REQUEST, Variance.RESPONSE}
        if name in self.request_only:
            return {Variance.REQUEST}
        if name in self.response_only:
            return {Variance.RESPONSE}
        return set()

    def to_dict(self) -> dict[str, list[str]]:
        return {
            "request_only": sorted(self.request_only),
            "response_only": sorted(self.response_only),
            "both": sorted(self.both),
            "unreachable": sorted(self.unreachable),
        }


def _refs_under(node: Any, depth: int = 0) -> Iterator[str]:
    """Component names referenced anywhere under a node.

    Iterative, with a depth guard on the stack entries rather than on the call
    stack, because a description that nests forty levels deep is a description
    somebody generated and it should still be walkable.
    """
    stack: list[tuple[Any, int]] = [(node, depth)]
    while stack:
        current, level = stack.pop()
        if level > 200:
            continue
        if isinstance(current, dict):
            ref = current.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
                yield ref.rsplit("/", 1)[-1]
            for value in current.values():
                stack.append((value, level + 1))
        elif isinstance(current, list):
            for value in current:
                stack.append((value, level + 1))


def reachability(document: Document) -> Reachability:
    """Classify every component schema by how it is reached."""
    schemas = document.schemas()
    seeds: list[tuple[str, Variance]] = []

    for op in document.operations():
        body = op.body.get("requestBody")
        if body is not None:
            _, resolved = document.deref(body, join(op.ptr, "requestBody"))
            for name in _refs_under(resolved):
                seeds.append((name, Variance.REQUEST))
        for param in op.body.get("parameters") or []:
            _, resolved = document.deref(param, op.ptr)
            for name in _refs_under(resolved):
                seeds.append((name, Variance.REQUEST))
        for param in document.path_item_parameters(op.path):
            _, resolved = document.deref(param, op.ptr)
            for name in _refs_under(resolved):
                seeds.append((name, Variance.REQUEST))
        responses = op.body.get("responses")
        if isinstance(responses, dict):
            for status, response in responses.items():
                _, resolved = document.deref(response, join(op.ptr, "responses", str(status)))
                for name in _refs_under(resolved):
                    seeds.append((name, Variance.RESPONSE))

    seen: set[tuple[str, Variance]] = set()
    work = list(seeds)
    while work:
        item = work.pop()
        if item in seen:
            continue
        seen.add(item)
        name, variance = item
        target = schemas.get(name)
        if target is None:
            continue
        for child in _refs_under(target):
            if (child, variance) not in seen:
                work.append((child, variance))

    result = Reachability()
    for name in schemas:
        in_request = (name, Variance.REQUEST) in seen
        in_response = (name, Variance.RESPONSE) in seen
        if in_request and in_response:
            result.both.add(name)
        elif in_request:
            result.request_only.add(name)
        elif in_response:
            result.response_only.add(name)
        else:
            result.unreachable.add(name)
    return result
