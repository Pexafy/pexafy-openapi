"""Semantic diff between two OpenAPI descriptions.

The comparison never expands ``$ref``. It walks both documents at once and, at
every schema, follows the refs on each side to a pointer pair. That pair plus
the variance is the memo key. A schema shared by nine responses is compared
once; a recursive schema terminates the first time it meets itself instead of
unrolling. Cost is linear in the number of distinct reachable pointer pairs,
which for a real description is a few thousand and does not depend on how
deeply the components nest. ``bench/bench_diff.py`` puts numbers on the
alternative.

Two normalisations happen before anything is compared, both of them there
because the naive answer produced a wall of false positives on a real upgrade:

``nullable``
    OpenAPI 3.0 spells it ``nullable: true``, 3.1 spells it ``type: ["string",
    "null"]``. A description that moved from 3.0 to 3.1 changes every nullable
    field in the document, and a differ that compares ``type`` as written
    reports a breaking type change on all of them. Types and nullability are
    pulled apart before comparison so the migration itself is silent and a real
    nullability change is still caught.

``paths``
    The same operation moved from an inline path item to a ``$ref``, or path
    parameters renamed from ``{id}`` to ``{photoId}``, is not a change to the
    contract. Path templates are compared after normalising parameter names,
    so renaming the placeholder is reported as cosmetic and not as one removed
    path plus one added path.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .document import Document, join, pointer
from .variance import Severity, Variance, severity_of

#: Ceiling on schema node pairs visited before the diff gives up and says so.
DEFAULT_MAX_NODES = 200_000
#: Ceiling on reported changes. A diff that produces more than this is not
#: going to be read; the count of what was dropped is more useful than the rest
#: of the list.
DEFAULT_MAX_CHANGES = 2_000

_UPPER_BOUNDS = ("maximum", "exclusiveMaximum", "maxLength", "maxItems", "maxProperties")
_LOWER_BOUNDS = ("minimum", "exclusiveMinimum", "minLength", "minItems", "minProperties")
_PROSE_KEYS = ("description", "title", "summary")
_EXAMPLE_KEYS = ("example", "examples")


@dataclass(frozen=True)
class Change:
    """One classified difference, anchored to a JSON Pointer."""

    code: str
    severity: Severity
    location: str
    message: str
    variance: Variance | None = None
    before: Any = None
    after: Any = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity.value,
            "variance": self.variance.value if self.variance else None,
            "location": self.location,
            "message": self.message,
            "before": self.before,
            "after": self.after,
        }

    def __str__(self) -> str:
        side = f" ({self.variance.value})" if self.variance else ""
        return f"{self.severity.value:9} {self.code}{side}\n    {self.location}\n    {self.message}"


@dataclass
class DiffReport:
    """What changed between two descriptions."""

    old_version: str
    new_version: str
    changes: list[Change] = field(default_factory=list)
    truncated: bool = False
    dropped: int = 0
    nodes_visited: int = 0

    def at_least(self, severity: Severity) -> list[Change]:
        return [c for c in self.changes if c.severity >= severity]

    @property
    def breaking(self) -> list[Change]:
        return [c for c in self.changes if c.severity is Severity.BREAKING]

    def counts(self) -> dict[str, int]:
        out = {s.value: 0 for s in Severity}
        for change in self.changes:
            out[change.severity.value] += 1
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "old_version": self.old_version,
            "new_version": self.new_version,
            "counts": self.counts(),
            "truncated": self.truncated,
            "dropped": self.dropped,
            "nodes_visited": self.nodes_visited,
            "changes": [c.to_dict() for c in self.changes],
        }

    def to_json(self, **kwargs: Any) -> str:
        return json.dumps(self.to_dict(), **kwargs)

    def to_markdown(self) -> str:
        """A pull request comment.

        Grouped by severity with the breaking ones first, because that is the
        only section anybody reads and burying it under forty cosmetic entries
        is how a bot comment gets collapsed and ignored.
        """
        lines = [f"### API diff {self.old_version} to {self.new_version}", ""]
        counts = self.counts()
        lines.append(
            " · ".join(f"**{counts[s.value]}** {s.value}" for s in Severity if counts[s.value])
            or "No differences."
        )
        for severity in Severity:
            group = [c for c in self.changes if c.severity is severity]
            if not group:
                continue
            lines += ["", f"#### {severity.value} ({len(group)})", ""]
            for change in group:
                side = f" _{change.variance.value}_" if change.variance else ""
                lines.append(f"- `{change.code}`{side} at `{change.location}` — {change.message}")
        if self.truncated:
            lines += ["", f"Truncated: {self.dropped} further differences were not reported."]
        return "\n".join(lines)

    def to_text(self) -> str:
        if not self.changes:
            return f"no differences between {self.old_version} and {self.new_version}"
        lines = [f"{self.old_version} -> {self.new_version}", ""]
        lines += [str(c) for c in self.changes]
        counts = self.counts()
        lines += ["", "  ".join(f"{counts[s.value]} {s.value}" for s in Severity)]
        if self.truncated:
            lines.append(f"truncated, {self.dropped} more not reported")
        return "\n".join(lines)


class _Differ:
    def __init__(
        self,
        old: Document,
        new: Document,
        *,
        max_nodes: int,
        max_changes: int,
        include_cosmetic: bool,
    ) -> None:
        self.old = old
        self.new = new
        self.max_nodes = max_nodes
        self.max_changes = max_changes
        self.include_cosmetic = include_cosmetic
        self.changes: list[Change] = []
        self.seen: set[tuple[str, str, str]] = set()
        self.nodes = 0
        self.dropped = 0
        self.exhausted = False

    # -- emitting --------------------------------------------------------

    def emit(
        self,
        code: str,
        location: str,
        message: str,
        variance: Variance | None = None,
        before: Any = None,
        after: Any = None,
    ) -> None:
        severity = severity_of(code, variance or Variance.RESPONSE)
        if severity is Severity.COSMETIC and not self.include_cosmetic:
            return
        if len(self.changes) >= self.max_changes:
            self.dropped += 1
            self.exhausted = True
            return
        self.changes.append(
            Change(
                code=code,
                severity=severity,
                location=location,
                message=message,
                variance=variance,
                before=_brief(before),
                after=_brief(after),
            )
        )

    # -- top level -------------------------------------------------------

    def run(self) -> DiffReport:
        self.info()
        self.servers()
        self.security(
            self.old.raw.get("security"), self.new.raw.get("security"), "/security", "the API"
        )
        self.security_schemes()
        self.paths()
        report = DiffReport(
            old_version=self.old.version,
            new_version=self.new.version,
            changes=_ordered(self.changes),
            truncated=self.exhausted,
            dropped=self.dropped,
            nodes_visited=self.nodes,
        )
        return report

    def info(self) -> None:
        if self.old.version != self.new.version:
            self.emit(
                "info.version.changed", "/info/version",
                f"{self.old.version} to {self.new.version}",
                before=self.old.version, after=self.new.version,
            )

    def servers(self) -> None:
        old = [s.get("url") for s in self.old.raw.get("servers", []) if isinstance(s, dict)]
        new = [s.get("url") for s in self.new.raw.get("servers", []) if isinstance(s, dict)]
        for url in old:
            if url not in new:
                self.emit(
                    "server.url.changed", "/servers",
                    f"{url} is no longer listed", before=url, after=new[:1],
                )

    def security(self, old: Any, new: Any, location: str, subject: str) -> None:
        """Compare a security requirement list.

        The list is OR of ANDs, so a requirement is satisfied by any one entry.
        Adding an entry widens what is accepted; removing the last one that a
        caller satisfies is what breaks them. Comparing by the set of scheme
        names inside each entry is coarse but it never reports a break that is
        not one, which matters more here than precision.
        """
        old_sets = _requirement_sets(old)
        new_sets = _requirement_sets(new)
        if old_sets == new_sets:
            return
        for entry in sorted(new_sets - old_sets):
            self.emit(
                "security.alternative.added", location,
                f"{subject} now also accepts {' + '.join(sorted(entry)) or 'anonymous'}",
                after=sorted(entry),
            )
        for entry in sorted(old_sets - new_sets):
            self.emit(
                "security.alternative.removed", location,
                f"{subject} no longer accepts {' + '.join(sorted(entry)) or 'anonymous'}",
                before=sorted(entry),
            )

    # -- paths and operations --------------------------------------------

    def security_schemes(self) -> None:
        """Compare the schemes themselves, not just which ones are required.

        A scheme that keeps its name and changes from a bearer token to an
        ``X-Api-Key`` header leaves the requirement lists untouched, so a diff
        that only reads ``security`` reports nothing at all while every caller
        starts getting a 401.
        """
        old = (self.old.raw.get("components") or {}).get("securitySchemes") or {}
        new = (self.new.raw.get("components") or {}).get("securitySchemes") or {}
        if not isinstance(old, dict) or not isinstance(new, dict):
            return
        for name in sorted(set(old) - set(new)):
            self.emit(
                "security.scheme.removed", pointer("components", "securitySchemes", name),
                f"security scheme {name} is gone", before=name,
            )
        for name in sorted(set(old) & set(new)):
            shape = ("type", "scheme", "in", "name", "bearerFormat", "openIdConnectUrl")
            before = {k: old[name].get(k) for k in shape if isinstance(old[name], dict)}
            after = {k: new[name].get(k) for k in shape if isinstance(new[name], dict)}
            if before != after:
                self.emit(
                    "security.scheme.changed", pointer("components", "securitySchemes", name),
                    f"security scheme {name} moved: {before} to {after}",
                    before=before, after=after,
                )

    def paths(self) -> None:
        old_ops = {(_template(o.path), o.method): o for o in self.old.operations()}
        new_ops = {(_template(o.path), o.method): o for o in self.new.operations()}
        # Templates are the comparison key; the real path is what a reader
        # needs to see in the pointer, so keep both.
        old_real = {t: o.path for (t, _), o in old_ops.items()}
        new_real = {t: o.path for (t, _), o in new_ops.items()}
        old_paths, new_paths = set(old_real), set(new_real)

        for template in sorted(old_paths - new_paths):
            path = old_real[template]
            self.emit("path.removed", pointer("paths", path), f"{path} is gone", before=path)
        for template in sorted(new_paths - old_paths):
            path = new_real[template]
            self.emit("path.added", pointer("paths", path), f"{path} is new", after=path)

        for key in sorted(set(old_ops) & set(new_ops)):
            self.operation(old_ops[key], new_ops[key])
        for key in sorted(set(old_ops) - set(new_ops)):
            if key[0] in new_paths:
                op = old_ops[key]
                self.emit("operation.removed", op.ptr, f"{op} is gone", before=str(op))
        for key in sorted(set(new_ops) - set(old_ops)):
            if key[0] in old_paths:
                op = new_ops[key]
                self.emit("operation.added", op.ptr, f"{op} is new", after=str(op))

    def operation(self, old_op, new_op) -> None:
        where = str(new_op)
        if old_op.operation_id != new_op.operation_id:
            self.emit(
                "operation.id.changed", join(new_op.ptr, "operationId"),
                f"{where}: {old_op.operation_id} to {new_op.operation_id}; every SDK built "
                "from this description renames a method",
                before=old_op.operation_id, after=new_op.operation_id,
            )
        if not old_op.body.get("deprecated") and new_op.body.get("deprecated"):
            self.emit("operation.deprecated", new_op.ptr, f"{where} is now deprecated")

        if "security" in old_op.body or "security" in new_op.body:
            self.security(
                old_op.body.get("security", self.old.raw.get("security")),
                new_op.body.get("security", self.new.raw.get("security")),
                join(new_op.ptr, "security"), where,
            )

        self.parameters(old_op, new_op, where)
        self.request_body(old_op, new_op, where)
        self.responses(old_op, new_op, where)

    def parameters(self, old_op, new_op, where: str) -> None:
        old_params = _parameters(self.old, old_op)
        new_params = _parameters(self.new, new_op)

        for key in sorted(set(new_params) - set(old_params)):
            param = new_params[key]
            required = bool(param.get("required"))
            code = "parameter.added.required" if required else "parameter.added.optional"
            self.emit(
                code, join(new_op.ptr, "parameters"),
                f"{where}: new {key[1]} parameter {key[0]}"
                + (" and it is required" if required else ""),
                Variance.REQUEST, after=key[0],
            )
        for key in sorted(set(old_params) - set(new_params)):
            moved = [k for k in new_params if k[0] == key[0]]
            if moved:
                self.emit(
                    "parameter.location.changed", join(new_op.ptr, "parameters"),
                    f"{where}: parameter {key[0]} moved from {key[1]} to {moved[0][1]}",
                    Variance.REQUEST, before=key[1], after=moved[0][1],
                )
            else:
                self.emit(
                    "parameter.removed", join(old_op.ptr, "parameters"),
                    f"{where}: parameter {key[0]} ({key[1]}) is gone",
                    Variance.REQUEST, before=key[0],
                )

        for key in sorted(set(old_params) & set(new_params)):
            old_p, new_p = old_params[key], new_params[key]
            loc = join(new_op.ptr, "parameters", key[0])
            if not old_p.get("required") and new_p.get("required"):
                self.emit(
                    "parameter.became.required", loc,
                    f"{where}: {key[0]} is now required", Variance.REQUEST,
                )
            elif old_p.get("required") and not new_p.get("required"):
                self.emit(
                    "parameter.became.optional", loc,
                    f"{where}: {key[0]} is no longer required", Variance.REQUEST,
                )
            self.schema(
                old_p.get("schema"), join(old_op.ptr, "parameters", key[0], "schema"),
                new_p.get("schema"), join(new_op.ptr, "parameters", key[0], "schema"),
                Variance.REQUEST, f"{where} parameter {key[0]}",
            )

    def request_body(self, old_op, new_op, where: str) -> None:
        old_body = _deref_or_none(self.old, old_op.body.get("requestBody"), old_op.ptr)
        new_body = _deref_or_none(self.new, new_op.body.get("requestBody"), new_op.ptr)
        if old_body is None and new_body is None:
            return
        if old_body is not None and new_body is None:
            self.emit(
                "request_body.removed", join(old_op.ptr, "requestBody"),
                f"{where}: the request body is gone", Variance.REQUEST,
            )
            return
        if old_body is None:
            if new_body.get("required"):
                self.emit(
                    "request_body.became.required", join(new_op.ptr, "requestBody"),
                    f"{where}: a request body is now required", Variance.REQUEST,
                )
            return
        if not old_body.get("required") and new_body.get("required"):
            self.emit(
                "request_body.became.required", join(new_op.ptr, "requestBody"),
                f"{where}: the request body is now required", Variance.REQUEST,
            )
        elif old_body.get("required") and not new_body.get("required"):
            self.emit(
                "request_body.became.optional", join(new_op.ptr, "requestBody"),
                f"{where}: the request body is now optional", Variance.REQUEST,
            )
        self.content(
            old_body.get("content"), join(old_op.ptr, "requestBody", "content"),
            new_body.get("content"), join(new_op.ptr, "requestBody", "content"),
            Variance.REQUEST, f"{where} request body",
        )

    def responses(self, old_op, new_op, where: str) -> None:
        old_r = old_op.body.get("responses") or {}
        new_r = new_op.body.get("responses") or {}
        if not isinstance(old_r, dict) or not isinstance(new_r, dict):
            return
        for status in sorted(set(old_r) - set(new_r)):
            self.emit(
                "response.status.removed", join(old_op.ptr, "responses", status),
                f"{where}: {status} is no longer documented", Variance.RESPONSE, before=status,
            )
        for status in sorted(set(new_r) - set(old_r)):
            self.emit(
                "response.status.added", join(new_op.ptr, "responses", status),
                f"{where}: {status} is new", Variance.RESPONSE, after=status,
            )
        for status in sorted(set(old_r) & set(new_r)):
            old_ptr = join(old_op.ptr, "responses", status)
            new_ptr = join(new_op.ptr, "responses", status)
            old_res = _deref_or_none(self.old, old_r[status], old_ptr) or {}
            new_res = _deref_or_none(self.new, new_r[status], new_ptr) or {}
            old_h = old_res.get("headers") or {}
            new_h = new_res.get("headers") or {}
            for name in sorted(set(old_h) - set(new_h)):
                self.emit(
                    "response.header.removed", join(old_ptr, "headers", name),
                    f"{where} {status}: header {name} is gone", Variance.RESPONSE, before=name,
                )
            for name in sorted(set(new_h) - set(old_h)):
                self.emit(
                    "response.header.added", join(new_ptr, "headers", name),
                    f"{where} {status}: header {name} is new", Variance.RESPONSE, after=name,
                )
            self.content(
                old_res.get("content"), join(old_ptr, "content"),
                new_res.get("content"), join(new_ptr, "content"),
                Variance.RESPONSE, f"{where} {status}",
            )

    def content(
        self, old: Any, old_ptr: str, new: Any, new_ptr: str,
        variance: Variance, where: str,
    ) -> None:
        old = old if isinstance(old, dict) else {}
        new = new if isinstance(new, dict) else {}
        for media in sorted(set(old) - set(new)):
            self.emit(
                "content_type.removed", join(old_ptr, media),
                f"{where}: {media} is no longer offered", variance, before=media,
            )
        for media in sorted(set(new) - set(old)):
            self.emit(
                "content_type.added", join(new_ptr, media),
                f"{where}: {media} is new", variance, after=media,
            )
        for media in sorted(set(old) & set(new)):
            self.schema(
                (old[media] or {}).get("schema"), join(old_ptr, media, "schema"),
                (new[media] or {}).get("schema"), join(new_ptr, media, "schema"),
                variance, f"{where} {media}",
            )

    # -- schemas ---------------------------------------------------------

    def schema(
        self, old: Any, old_ptr: str, new: Any, new_ptr: str,
        variance: Variance, where: str,
    ) -> None:
        if old is None and new is None:
            return
        self.nodes += 1
        if self.nodes > self.max_nodes:
            self.exhausted = True
            return

        old_ptr, old = self.old.deref(old, old_ptr) if old is not None else (old_ptr, None)
        new_ptr, new = self.new.deref(new, new_ptr) if new is not None else (new_ptr, None)

        key = (old_ptr, new_ptr, variance.value)
        if key in self.seen:
            return
        self.seen.add(key)

        if not isinstance(old, dict) or not isinstance(new, dict):
            return

        self.types(old, new, new_ptr, variance, where)
        self.enums(old, new, new_ptr, variance, where)
        self.bounds(old, new, new_ptr, variance, where)
        self.flags(old, new, new_ptr, variance, where)
        self.prose(old, new, new_ptr, variance, where)
        self.properties(old, old_ptr, new, new_ptr, variance, where)
        self.composition(old, old_ptr, new, new_ptr, variance, where)

        self.schema(
            old.get("items"), join(old_ptr, "items"),
            new.get("items"), join(new_ptr, "items"),
            variance, f"{where}[]",
        )

    def types(self, old: dict, new: dict, ptr: str, variance: Variance, where: str) -> None:
        old_types, old_null = _types(old)
        new_types, new_null = _types(new)
        if old_null != new_null:
            code = "nullable.added" if new_null else "nullable.removed"
            self.emit(code, ptr, f"{where} is {'now' if new_null else 'no longer'} nullable",
                      variance, before=old_null, after=new_null)
        if old_types == new_types or not old_types or not new_types:
            return
        if old_types < new_types:
            code = "type.widened"
        elif new_types < old_types:
            code = "type.narrowed"
        else:
            code = "type.changed"
        self.emit(
            code, ptr,
            f"{where}: {_join(old_types)} to {_join(new_types)}",
            variance, before=sorted(old_types), after=sorted(new_types),
        )

    def enums(self, old: dict, new: dict, ptr: str, variance: Variance, where: str) -> None:
        old_enum = old.get("enum")
        new_enum = new.get("enum")
        if not isinstance(old_enum, list) and not isinstance(new_enum, list):
            self._format(old, new, ptr, variance, where)
            return
        if not isinstance(old_enum, list):
            self.emit(
                "enum.introduced", ptr,
                f"{where} is now restricted to {_join(_hashable(new_enum))}",
                variance, after=new_enum,
            )
            return
        if not isinstance(new_enum, list):
            self.emit(
                "enum.dropped", ptr, f"{where} is no longer a closed set",
                variance, before=old_enum,
            )
            return
        old_set, new_set = _hashable(old_enum), _hashable(new_enum)
        added = new_set - old_set
        removed = old_set - new_set
        if added:
            self.emit(
                "enum.value.added", ptr,
                f"{where} gained {_join(added)}", variance, after=sorted(added, key=str),
            )
        if removed:
            self.emit(
                "enum.value.removed", ptr,
                f"{where} lost {_join(removed)}", variance, before=sorted(removed, key=str),
            )
        self._format(old, new, ptr, variance, where)

    def _format(self, old: dict, new: dict, ptr: str, variance: Variance, where: str) -> None:
        if old.get("format") != new.get("format"):
            self.emit(
                "format.changed", ptr,
                f"{where}: format {old.get('format')} to {new.get('format')}",
                variance, before=old.get("format"), after=new.get("format"),
            )

    def bounds(self, old: dict, new: dict, ptr: str, variance: Variance, where: str) -> None:
        for keyword in _UPPER_BOUNDS + _LOWER_BOUNDS:
            before, after = old.get(keyword), new.get(keyword)
            if before == after:
                continue
            upper = keyword in _UPPER_BOUNDS
            if before is None:
                tightened = True
            elif after is None:
                tightened = False
            elif isinstance(before, (int, float)) and isinstance(after, (int, float)):
                tightened = after < before if upper else after > before
            else:
                tightened = True
            code = "bound.tightened" if tightened else "bound.relaxed"
            self.emit(
                code, ptr, f"{where}: {keyword} {before} to {after}",
                variance, before=before, after=after,
            )
        for keyword in ("pattern", "multipleOf"):
            before, after = old.get(keyword), new.get(keyword)
            if before == after:
                continue
            # A pattern change cannot be shown to be a relaxation without
            # deciding regular expression containment, so it is reported as a
            # tightening. Over-reporting a break is recoverable with a waiver;
            # under-reporting one is a production incident.
            code = "bound.relaxed" if after is None else "bound.tightened"
            self.emit(
                code, ptr, f"{where}: {keyword} {before!r} to {after!r}",
                variance, before=before, after=after,
            )

    def flags(self, old: dict, new: dict, ptr: str, variance: Variance, where: str) -> None:
        if old.get("default") != new.get("default"):
            self.emit(
                "default.changed", ptr,
                f"{where}: default {old.get('default')!r} to {new.get('default')!r}",
                variance, before=old.get("default"), after=new.get("default"),
            )
        for keyword in ("readOnly", "writeOnly"):
            if bool(old.get(keyword)) != bool(new.get(keyword)):
                self.emit(
                    "read_write_only.changed", ptr,
                    f"{where}: {keyword} {bool(old.get(keyword))} to {bool(new.get(keyword))}",
                    variance, before=old.get(keyword), after=new.get(keyword),
                )
        old_disc = (old.get("discriminator") or {}).get("propertyName")
        new_disc = (new.get("discriminator") or {}).get("propertyName")
        if old_disc != new_disc:
            self.emit(
                "discriminator.changed", ptr,
                f"{where}: discriminator {old_disc} to {new_disc}",
                variance, before=old_disc, after=new_disc,
            )

    def prose(self, old: dict, new: dict, ptr: str, variance: Variance, where: str) -> None:
        if not self.include_cosmetic:
            return
        for keyword in _PROSE_KEYS:
            if old.get(keyword) != new.get(keyword):
                self.emit("description.changed", ptr, f"{where}: {keyword} rewritten", variance)
        for keyword in _EXAMPLE_KEYS:
            if old.get(keyword) != new.get(keyword):
                self.emit("example.changed", ptr, f"{where}: {keyword} changed", variance)

    def properties(
        self, old: dict, old_ptr: str, new: dict, new_ptr: str,
        variance: Variance, where: str,
    ) -> None:
        old_props = old.get("properties") if isinstance(old.get("properties"), dict) else {}
        new_props = new.get("properties") if isinstance(new.get("properties"), dict) else {}
        old_required = set(old.get("required") or [])
        new_required = set(new.get("required") or [])

        old_ap, new_ap = old.get("additionalProperties"), new.get("additionalProperties")
        if (old_ap is False) != (new_ap is False):
            code = (
                "additional_properties.closed" if new_ap is False
                else "additional_properties.opened"
            )
            self.emit(
                code, new_ptr,
                f"{where}: additionalProperties {old_ap!r} to {new_ap!r}",
                variance, before=old_ap, after=new_ap,
            )

        for name in sorted(set(new_props) - set(old_props)):
            required = name in new_required
            code = "property.added.required" if required else "property.added.optional"
            self.emit(
                code, join(new_ptr, "properties", name),
                f"{where}.{name} is new" + (" and required" if required else ""),
                variance, after=name,
            )
        for name in sorted(set(old_props) - set(new_props)):
            # The *new* flag decides. A caller who keeps sending a removed
            # field is ignored if the object still tolerates extras and gets a
            # 422 if it does not, and which of those happens is decided by the
            # schema they are validated against today.
            code = "property.removed.closed" if new_ap is False else "property.removed"
            self.emit(
                code, join(old_ptr, "properties", name),
                f"{where}.{name} is gone", variance, before=name,
            )
        for name in sorted(set(old_props) & set(new_props)):
            was, now = name in old_required, name in new_required
            if was != now:
                code = "property.became.required" if now else "property.became.optional"
                self.emit(
                    code, join(new_ptr, "properties", name),
                    f"{where}.{name} is {'now' if now else 'no longer'} required", variance,
                )
            self.schema(
                old_props[name], join(old_ptr, "properties", name),
                new_props[name], join(new_ptr, "properties", name),
                variance, f"{where}.{name}",
            )

    def composition(
        self, old: dict, old_ptr: str, new: dict, new_ptr: str,
        variance: Variance, where: str,
    ) -> None:
        """Compare allOf / oneOf / anyOf positionally.

        Deciding whether two unions describe the same set of documents is a
        subtyping problem this package does not attempt. Members are paired by
        index and a change in arity is reported as notable so a human looks at
        it. Reordering a oneOf therefore produces noise; that is the honest
        trade against claiming a break that is not there.
        """
        for keyword in ("allOf", "oneOf", "anyOf"):
            old_list = old.get(keyword) if isinstance(old.get(keyword), list) else []
            new_list = new.get(keyword) if isinstance(new.get(keyword), list) else []
            if len(old_list) != len(new_list):
                self.emit(
                    "composition.changed", join(new_ptr, keyword),
                    f"{where}: {keyword} went from {len(old_list)} to {len(new_list)} members",
                    variance, before=len(old_list), after=len(new_list),
                )
            for index in range(min(len(old_list), len(new_list))):
                self.schema(
                    old_list[index], join(old_ptr, keyword, index),
                    new_list[index], join(new_ptr, keyword, index),
                    variance, f"{where}/{keyword}[{index}]",
                )


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _types(schema: dict) -> tuple[frozenset, bool]:
    """Split ``type`` into a set of real types plus a nullability flag.

    Handles both spellings so that migrating a description from OpenAPI 3.0 to
    3.1 does not read as a breaking type change on every optional field.
    """
    raw = schema.get("type")
    if raw is None:
        types: set[str] = set()
    elif isinstance(raw, str):
        types = {raw}
    elif isinstance(raw, list):
        types = {t for t in raw if isinstance(t, str)}
    else:
        types = set()
    nullable = "null" in types or bool(schema.get("nullable"))
    types.discard("null")
    return frozenset(types), nullable


def _hashable(values: Any) -> set:
    out = set()
    for value in values or []:
        try:
            hash(value)
        except TypeError:
            out.add(json.dumps(value, sort_keys=True))
        else:
            out.add(value)
    return out


def _join(values) -> str:
    return ", ".join(str(v) for v in sorted(values, key=str)) or "nothing"


def _brief(value: Any, limit: int = 200) -> Any:
    """Keep before/after small enough to put in a comment."""
    if isinstance(value, (dict, list)):
        text = json.dumps(value, sort_keys=True, default=str)
        return text if len(text) <= limit else text[:limit] + "…"
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + "…"
    return value


def _template(path: str) -> str:
    """Normalise ``{whatever}`` to ``{}`` so renaming a placeholder is not a diff."""
    out, depth = [], 0
    for char in path:
        if char == "{":
            depth += 1
            if depth == 1:
                out.append("{")
        elif char == "}":
            if depth == 1:
                out.append("}")
            depth = max(0, depth - 1)
        elif depth == 0:
            out.append(char)
    return "".join(out)


def _requirement_sets(requirements: Any) -> set[frozenset]:
    if not isinstance(requirements, list):
        return set()
    out = set()
    for entry in requirements:
        if isinstance(entry, dict):
            out.add(frozenset(entry))
    return out


def _deref_or_none(document: Document, node: Any, ptr: str) -> dict | None:
    if node is None:
        return None
    _, resolved = document.deref(node, ptr)
    return resolved if isinstance(resolved, dict) else None


def _parameters(document: Document, op) -> dict[tuple[str, str], dict]:
    """Operation parameters merged with the ones inherited from the path item.

    An operation-level parameter with the same name and location overrides the
    inherited one, which is what the specification says and what a differ that
    only looks at ``operation.parameters`` gets wrong the first time somebody
    hoists a shared header up to the path.
    """
    merged: dict[tuple[str, str], dict] = {}
    for source in (document.path_item_parameters(op.path), op.body.get("parameters") or []):
        if not isinstance(source, list):
            continue
        for entry in source:
            _, param = document.deref(entry, op.ptr)
            if isinstance(param, dict) and isinstance(param.get("name"), str):
                merged[(param["name"], param.get("in", "query"))] = param
    return merged


def _ordered(changes: list[Change]) -> list[Change]:
    return sorted(changes, key=lambda c: (-c.severity.rank, c.location, c.code))


def diff(
    old: Document | dict | str,
    new: Document | dict | str,
    *,
    include_cosmetic: bool = False,
    max_nodes: int = DEFAULT_MAX_NODES,
    max_changes: int = DEFAULT_MAX_CHANGES,
) -> DiffReport:
    """Compare two descriptions and classify every difference.

    Cosmetic changes are excluded by default. On a description that is actively
    maintained they are most of the diff, and a report where the one breaking
    change sits below thirty rewritten descriptions is a report nobody reads to
    the end.

    Neither ceiling raises. A diff that hits ``max_nodes`` or ``max_changes``
    comes back with ``truncated`` set and the changes it did find, because the
    caller is usually a CI job whose job is to say something useful about a pull
    request rather than to be right about all of it.
    """
    return _Differ(
        _document(old), _document(new),
        max_nodes=max_nodes, max_changes=max_changes, include_cosmetic=include_cosmetic,
    ).run()


def _document(value: Document | dict | str) -> Document:
    if isinstance(value, Document):
        return value
    if isinstance(value, dict):
        return Document.from_dict(value)
    return Document.from_file(value)
