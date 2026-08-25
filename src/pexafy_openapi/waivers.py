"""Accepted breaking changes, with a reason and an expiry.

A gate that can only say no gets turned off. Sooner or later a breaking change
is the right call — a field was wrong, an enum has to grow, a parameter was
never usable — and at that point the team either disables the check for one
merge and forgets to turn it back on, or edits the baseline to make the finding
disappear, which loses the fact that a decision was taken at all.

So breaking changes are waived explicitly. A waiver names the code, points at
the location, carries the reason in prose and carries a date after which it
stops working. Expiry is the part that matters: without it the waiver file
becomes a graveyard, and the day somebody makes the same edit somewhere else
the gate stays quiet because a two-year-old entry still matches. An expired
waiver is reported as a finding of its own rather than silently ignored, which
is what forces the file to be read.

Locations are matched by glob against the JSON Pointer, so
``/paths/~1search~1photos/*`` waives a code across one endpoint without waiving
it across the API.

    {
      "waivers": [
        {
          "code": "enum.value.added",
          "location": "/components/schemas/*",
          "reason": "wikimedia became a source; consumers were warned in the changelog",
          "expires": "2027-01-01"
        }
      ]
    }
"""

from __future__ import annotations

import fnmatch
import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from .diff import Change, DiffReport
from .errors import LoadError
from .variance import Severity


@dataclass(frozen=True)
class Waiver:
    code: str
    location: str = "*"
    reason: str = ""
    expires: date | None = None

    def matches(self, change: Change) -> bool:
        if not fnmatch.fnmatchcase(change.code, self.code):
            return False
        return fnmatch.fnmatchcase(change.location, self.location)

    def expired(self, today: date) -> bool:
        return self.expires is not None and self.expires < today

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "location": self.location,
            "reason": self.reason,
            "expires": self.expires.isoformat() if self.expires else None,
        }


@dataclass
class WaiverResult:
    """What survived the waiver file."""

    remaining: list[Change]
    waived: list[tuple[Change, Waiver]]
    expired: list[Waiver]
    unused: list[Waiver]

    def to_dict(self) -> dict[str, Any]:
        return {
            "remaining": [c.to_dict() for c in self.remaining],
            "waived": [
                {"change": c.to_dict(), "waiver": w.to_dict()} for c, w in self.waived
            ],
            "expired": [w.to_dict() for w in self.expired],
            "unused": [w.to_dict() for w in self.unused],
        }


def parse_waivers(payload: Any, *, origin: str = "<memory>") -> list[Waiver]:
    if isinstance(payload, dict):
        entries = payload.get("waivers", [])
    elif isinstance(payload, list):
        entries = payload
    else:
        raise LoadError(f"{origin}: waiver file is neither a list nor an object")
    if not isinstance(entries, list):
        raise LoadError(f"{origin}: 'waivers' is not a list")

    out: list[Waiver] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict) or "code" not in entry:
            raise LoadError(f"{origin}: waiver {index} has no 'code'")
        if not str(entry.get("reason", "")).strip():
            # A waiver without a reason is a mute button. The reason is the
            # entire point of writing the file down instead of lowering the
            # threshold, so it is required rather than encouraged.
            raise LoadError(f"{origin}: waiver {index} ({entry['code']}) has no reason")
        raw_expiry = entry.get("expires")
        expires: date | None = None
        if raw_expiry:
            try:
                expires = date.fromisoformat(str(raw_expiry))
            except ValueError as exc:
                raise LoadError(f"{origin}: waiver {index} has a bad date {raw_expiry!r}") from exc
        out.append(
            Waiver(
                code=str(entry["code"]),
                location=str(entry.get("location", "*")),
                reason=str(entry["reason"]),
                expires=expires,
            )
        )
    return out


def load_waivers(path: str | Path) -> list[Waiver]:
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise LoadError(f"{p}: {exc}") from exc
    try:
        payload = json.loads(text)
    except ValueError as exc:
        raise LoadError(f"{p}: {exc}") from exc
    return parse_waivers(payload, origin=str(p))


def apply_waivers(
    report: DiffReport,
    waivers: list[Waiver],
    *,
    threshold: Severity = Severity.BREAKING,
    today: date | None = None,
) -> WaiverResult:
    """Split a report into what a waiver covers and what nobody has accepted.

    Only changes at or above ``threshold`` can be waived. Waiving a cosmetic
    change is meaningless and a file full of them hides the two entries that
    matter.
    """
    today = today or date.today()
    live = [w for w in waivers if not w.expired(today)]
    expired = [w for w in waivers if w.expired(today)]

    remaining: list[Change] = []
    waived: list[tuple[Change, Waiver]] = []
    used: set[int] = set()

    for change in report.at_least(threshold):
        for index, waiver in enumerate(live):
            if waiver.matches(change):
                waived.append((change, waiver))
                used.add(index)
                break
        else:
            remaining.append(change)

    unused = [w for index, w in enumerate(live) if index not in used]
    return WaiverResult(remaining=remaining, waived=waived, expired=expired, unused=unused)
