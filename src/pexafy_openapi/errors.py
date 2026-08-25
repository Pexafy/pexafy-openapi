"""Exceptions raised while loading or walking a description."""

from __future__ import annotations


class SpecError(Exception):
    """Base class for everything this package raises."""


class LoadError(SpecError):
    """The file could not be read, parsed, or is not an OpenAPI document."""


class PointerError(SpecError):
    """A JSON Pointer does not resolve inside the document."""


class RefError(SpecError):
    """A ``$ref`` could not be followed.

    External refs (anything with a non-empty URI part) are refused rather than
    fetched. A diff that silently reaches across the network gives a different
    answer depending on where it runs, which makes it useless in CI.
    """


class BudgetExceeded(SpecError):
    """A traversal hit its node ceiling before finishing.

    Raised only by the eager operations. The diff never raises this: it reports
    a truncated result instead, because a partial answer in a pull request
    comment is more useful than a stack trace.
    """
