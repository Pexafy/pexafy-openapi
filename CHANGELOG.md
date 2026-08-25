# Changelog

Versions here are versions of the API description. The tooling ships with the
same number so that a pinned version of the package always understands the
description it was released beside.

## 1.3.0

Wikimedia Commons is indexed, so `Source` gained `wikimedia`. This is a
breaking change for consumers whose client type for `source` is a closed set,
which is most consumers whose SDK came out of a code generator. It was
announced in the 1.2.0 notes and it is recorded in `waivers.json` rather than
made to disappear.

`score_threshold` on both search endpoints, so a caller can decide that a
semantic search returned nothing useful. Search always returns something; there
is no natural zero-result case to detect.

`per_page` now defaults to 20 rather than 24. Nothing in the wire contract
moved, and every caller who never set the parameter gets a different page size,
which is why the diff calls it notable rather than additive.

## 1.2.0

`/facets/licenses`, so the licence families are readable rather than guessable.
`sort_by` on the search endpoints. `blur_hash` on `Photo`.

Every extensible enum gained `x-extensible-enum`, which is what lets a
generator emit an open type. It should have been there from the start; adding
it later does not help anybody who already generated a client.

## 1.1.0

First published description.
