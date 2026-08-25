"""Loading an OpenAPI document and moving around inside it.

Two things here are less obvious than they look.

The first is JSON Pointer escaping. Every path in an OpenAPI document is a
segment containing slashes, and half of them contain braces: ``/photos/{id}``.
Written into a pointer that becomes ``/paths/~1photos~1{id}``, and a tool that
forgets the escape produces pointers that look right, sort right, and resolve
to nothing. Anchoring a finding to the wrong place is worse than not anchoring
it, so both directions are implemented here and tested.

The second is ``$ref`` resolution. Resolution is lazy: :meth:`Document.deref`
takes one node and hands back the pointer it resolved to plus the node itself,
and callers decide whether to keep walking. Nothing in this package expands a
document in place during a diff. :func:`inline_refs` exists so the cost of
doing it that way can be measured (``bench/bench_diff.py``), and it carries a
node budget because on a real description it does not terminate in any useful
amount of memory.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import BudgetExceeded, LoadError, PointerError, RefError

HTTP_METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")

# Enough to hold anything sane, low enough that a pathological document fails
# in a second rather than swapping the machine.
DEFAULT_NODE_BUDGET = 2_000_000


def escape(segment: str) -> str:
    """Escape one path segment for use in a JSON Pointer (RFC 6901)."""
    return segment.replace("~", "~0").replace("/", "~1")


def unescape(segment: str) -> str:
    """Reverse :func:`escape`.

    Order matters: ``~01`` must come back as ``~1`` and not as ``/``, so ``~1``
    is expanded before ``~0``.
    """
    return segment.replace("~1", "/").replace("~0", "~")


def pointer(*segments: str | int) -> str:
    """Build a pointer from already-unescaped segments."""
    return "".join("/" + escape(str(s)) for s in segments)


def join(base: str, *segments: str | int) -> str:
    return base + pointer(*segments)


def split(ptr: str) -> list[str]:
    if ptr in ("", "#"):
        return []
    if ptr.startswith("#"):
        ptr = ptr[1:]
    if not ptr.startswith("/"):
        raise PointerError(f"pointer must start with '/': {ptr!r}")
    return [unescape(part) for part in ptr.split("/")[1:]]


def resolve_pointer(root: Any, ptr: str) -> Any:
    node = root
    for part in split(ptr):
        if isinstance(node, list):
            try:
                node = node[int(part)]
            except (ValueError, IndexError) as exc:
                raise PointerError(f"{ptr} does not resolve at {part!r}") from exc
        elif isinstance(node, dict):
            if part not in node:
                raise PointerError(f"{ptr} does not resolve at {part!r}")
            node = node[part]
        else:
            raise PointerError(f"{ptr} runs past a leaf at {part!r}")
    return node


@dataclass(frozen=True)
class Operation:
    """One method on one path, with its pointer already built."""

    path: str
    method: str
    body: dict[str, Any]
    ptr: str

    @property
    def operation_id(self) -> str | None:
        value = self.body.get("operationId")
        return value if isinstance(value, str) else None

    def __str__(self) -> str:
        return f"{self.method.upper()} {self.path}"


class Document:
    """An OpenAPI description held as plain data.

    No schema validation happens on load. A description that is malformed in a
    way that matters shows up as a lint finding with a pointer to the offending
    node, which is more useful than a validator traceback naming a JSON path
    twelve levels deep.
    """

    def __init__(self, raw: dict[str, Any], *, origin: str = "<memory>") -> None:
        if not isinstance(raw, dict):
            raise LoadError(f"{origin}: top level is {type(raw).__name__}, expected an object")
        if "openapi" not in raw and "swagger" not in raw:
            raise LoadError(f"{origin}: no 'openapi' key, this is not an OpenAPI document")
        if "swagger" in raw and "openapi" not in raw:
            raise LoadError(f"{origin}: Swagger 2.0 is not supported, convert to OpenAPI 3 first")
        self.raw = raw
        self.origin = origin
        self._deref_cache: dict[str, tuple[str, Any]] = {}

    # -- loading ---------------------------------------------------------

    @classmethod
    def from_dict(cls, raw: dict[str, Any], *, origin: str = "<memory>") -> Document:
        return cls(raw, origin=origin)

    @classmethod
    def from_text(cls, text: str, *, origin: str = "<string>") -> Document:
        text = text.lstrip("﻿")
        stripped = text.lstrip()
        if stripped.startswith(("{", "[")):
            try:
                return cls(json.loads(text), origin=origin)
            except ValueError as exc:
                raise LoadError(f"{origin}: {exc}") from exc
        try:
            import yaml
        except ImportError as exc:  # pragma: no cover - depends on the environment
            raise LoadError(
                f"{origin}: looks like YAML but PyYAML is not installed "
                "(pip install 'pexafy-openapi[yaml]')"
            ) from exc
        try:
            return cls(yaml.safe_load(text), origin=origin)
        except yaml.YAMLError as exc:
            raise LoadError(f"{origin}: {exc}") from exc

    @classmethod
    def from_file(cls, path: str | os.PathLike) -> Document:
        p = Path(path)
        try:
            text = p.read_text(encoding="utf-8")
        except OSError as exc:
            raise LoadError(f"{p}: {exc}") from exc
        return cls.from_text(text, origin=str(p))

    # -- identity --------------------------------------------------------

    @property
    def version(self) -> str:
        info = self.raw.get("info")
        if isinstance(info, dict) and isinstance(info.get("version"), str):
            return info["version"]
        return "0.0.0"

    @property
    def title(self) -> str:
        info = self.raw.get("info")
        if isinstance(info, dict) and isinstance(info.get("title"), str):
            return info["title"]
        return "untitled"

    # -- traversal -------------------------------------------------------

    def deref(self, node: Any, ptr: str) -> tuple[str, Any]:
        """Follow ``$ref`` chains until the node is something else.

        Returns the pointer the node finally lives at and the node. For a node
        that is not a ref the input is handed straight back, so callers can
        treat every node the same way.
        """
        if not isinstance(node, dict) or "$ref" not in node:
            return ptr, node

        seen: list[str] = []
        current, current_ptr = node, ptr
        while isinstance(current, dict) and "$ref" in current:
            ref = current["$ref"]
            if not isinstance(ref, str):
                raise RefError(f"{current_ptr}: $ref is not a string")
            if not ref.startswith("#"):
                raise RefError(
                    f"{current_ptr}: external ref {ref!r}; bundle the document first "
                    "so the diff does not depend on what the network returns"
                )
            target = ref[1:] or ""
            if target in seen:
                raise RefError(f"{ptr}: $ref cycle through {' -> '.join(seen + [target])}")
            seen.append(target)
            cached = self._deref_cache.get(target)
            if cached is not None:
                return cached
            current = resolve_pointer(self.raw, target)
            current_ptr = target
        result = (current_ptr, current)
        for target in seen:
            self._deref_cache[target] = result
        return result

    def operations(self) -> Iterator[Operation]:
        """Every path/method pair, in document order.

        ``$ref`` at the path-item level is followed, which is rare but legal and
        is exactly the kind of thing a description picks up once somebody starts
        sharing path items between two APIs.
        """
        paths = self.raw.get("paths")
        if not isinstance(paths, dict):
            return
        for path, item in paths.items():
            if not isinstance(item, dict):
                continue
            item_ptr = pointer("paths", path)
            item_ptr, item = self.deref(item, item_ptr)
            if not isinstance(item, dict):
                continue
            for method in HTTP_METHODS:
                body = item.get(method)
                if isinstance(body, dict):
                    yield Operation(path, method, body, join(item_ptr, method))

    def path_item_parameters(self, path: str) -> list[Any]:
        """Parameters declared on the path item rather than on the operation.

        These are inherited by every method on the path. Forgetting them is how
        a diff reports that an authentication header was removed from six
        operations at once when it merely moved up one level.
        """
        paths = self.raw.get("paths")
        if not isinstance(paths, dict):
            return []
        item = paths.get(path)
        if not isinstance(item, dict):
            return []
        _, item = self.deref(item, pointer("paths", path))
        params = item.get("parameters") if isinstance(item, dict) else None
        return list(params) if isinstance(params, list) else []

    def schemas(self) -> dict[str, Any]:
        components = self.raw.get("components")
        if not isinstance(components, dict):
            return {}
        schemas = components.get("schemas")
        return schemas if isinstance(schemas, dict) else {}

    def count_nodes(self) -> int:
        """Size of the document as the traversals see it."""
        total = 0
        stack: list[Any] = [self.raw]
        while stack:
            node = stack.pop()
            total += 1
            if isinstance(node, dict):
                stack.extend(node.values())
            elif isinstance(node, list):
                stack.extend(node)
        return total


def inline_refs(
    document: Document,
    *,
    max_depth: int = 12,
    node_budget: int = DEFAULT_NODE_BUDGET,
) -> dict[str, Any]:
    """Expand every internal ``$ref`` into a self-contained tree.

    This is the approach most tools take before comparing two documents, and it
    is the reason they fall over on descriptions that are not small. Sharing is
    what keeps an OpenAPI document compact: one ``Photo`` schema referenced from
    nine responses is one subtree in the file and nine copies after inlining.
    Nest that two more levels and the copies multiply. ``bench/bench_diff.py``
    measures where it stops being viable; the short version is that the cost is
    exponential in nesting depth while the diff in :mod:`pexafy_openapi.diff` is
    linear in document size.

    Recursive schemas have no expansion at all, so ``max_depth`` truncates them
    to ``{}`` rather than pretending. ``node_budget`` raises
    :class:`~pexafy_openapi.errors.BudgetExceeded` instead of letting the
    process grow until the kernel intervenes.
    """
    produced = 0

    def expand(node: Any, ptr: str, depth: int, chain: tuple[str, ...]) -> Any:
        nonlocal produced
        produced += 1
        if produced > node_budget:
            raise BudgetExceeded(
                f"inlining {document.origin} passed {node_budget} nodes; "
                "the document is recursive or too shared to expand"
            )
        if isinstance(node, dict):
            if "$ref" in node:
                if depth >= max_depth:
                    return {}
                target_ptr, target = document.deref(node, ptr)
                if target_ptr in chain:
                    return {}
                return expand(target, target_ptr, depth + 1, chain + (target_ptr,))
            return {k: expand(v, join(ptr, k), depth, chain) for k, v in node.items()}
        if isinstance(node, list):
            return [expand(v, join(ptr, i), depth, chain) for i, v in enumerate(node)]
        return node

    return expand(document.raw, "", 0, ())
