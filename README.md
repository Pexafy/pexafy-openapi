# pexafy-openapi

The public OpenAPI description of the Pexafy Stock Photos API, versioned, plus
the tool that stops it from lying. The tool exists because the usual question,
did this release break anybody, has an answer that flips depending on whether
the schema you edited sits under a request or under a response, and almost
nothing checks which.

Adding a value to an enum is the example everybody gets wrong. In a request it
is additive: one more thing the server accepts. In a response it is breaking,
because a consumer whose client types came out of a code generator for Java,
Kotlin, C# or Rust deserialises a closed set and raises on a value it has never
seen. Their build does not break. Their tests do not break. Their integration
falls over in production on the day the first row carrying the new value is
indexed. If you have shipped a public API for long enough, you have done this to
somebody.

## What is in here

`openapi/v1.json` is the description itself, with `openapi/releases/` holding
every published version so a diff has something to diff against.
`src/pexafy_openapi/` is a dependency-free package that compares two
descriptions, classifies every difference by which side of the call it is on,
and gates a release against a baseline with an explicit file of accepted
breaking changes.

## Install

```bash
pip install pexafy-openapi
pip install git+https://github.com/Pexafy/pexafy-openapi   # or from source
```

Nothing is required beyond the standard library. `pip install
'pexafy-openapi[yaml]'` adds PyYAML if you keep your descriptions in YAML.

## The smallest thing that runs

```bash
pexafy-openapi diff openapi/releases/1.2.0.json openapi/v1.json
```

```
breaking  enum.value.added (response)
    /components/schemas/Source
    GET /facets/photographers/suggest 200 application/json.data[].source gained wikimedia
...
additive  enum.value.added (request)
    /components/schemas/Source
    GET /photos/{photo_id}/similar parameter source[] gained wikimedia
```

One edit to one component, reported twice with opposite verdicts, because
`Source` is a filter on the way in and a field on the way out. That is the
output no variance-blind differ can produce: there is one change and two correct
answers, so a classifier that does not know which route it took has to pick one
and be wrong about the other.

The same thing from Python:

```python
from pexafy_openapi import Document, diff

old = Document.from_file("openapi/releases/1.2.0.json")
new = Document.from_file("openapi/v1.json")

for change in diff(old, new).breaking:
    print(change.code, change.variance, change.location)

print(diff(old, new).to_markdown())
```

`diff(old, new)` takes paths, dicts or `Document` objects. `Document.from_file`
handles JSON and, with PyYAML installed, YAML.

## How wrong is the usual heuristic

Most breaking-change detection reduces to one rule: additions are safe, removals
break people. `bench/bench_variance.py` applies fourteen edits to a working
document twice each, once in a request position and once in a response position,
runs the diff for real and compares the verdicts. Results in
`bench/results-variance.csv`:

| outcome | count |
| --- | --- |
| agree | 16 |
| heuristic says breaking, it is not | 6 |
| heuristic says safe, it breaks consumers | 6 |

Twelve of twenty-eight positions disagree. The six in the last row are the ones
that cost money, because a tool that says "safe" is a tool nobody reviews:
adding a required property to a request, making a response property optional,
adding an enum value to a response, widening a response type, allowing null in a
response, and changing a response `format` from `int32` to `int64`.

The last one is worth dwelling on. Nothing about the wire format changes. A
number that used to fit in a signed 32-bit field now might not, and the consumer
finds out when the first identifier crosses two billion.

Reproduce it with `python bench/bench_variance.py`. The full rule table, in both
columns, is `pexafy-openapi rules`, or `rule_table()` from Python.

## Why the diff does not resolve `$ref` first

The obvious implementation resolves every `$ref` into a self-contained tree and
compares two plain dictionaries. It is a hundred lines and it is what most
tooling does. It also multiplies the document by the fan-out of every level,
which is exactly the sharing that made the description compact in the first
place.

`bench/bench_diff.py` measures it on documents whose only variable is nesting
depth. From `bench/results-scaling.csv`, Python 3.12 on Linux:

| depth | nodes in the file | nodes after inlining | time to inline | peak while inlining | this diff |
| --- | --- | --- | --- | --- | --- |
| 4 | 218 | 7,181 | 7 ms | 0.4 MB | 0.5 ms |
| 6 | 244 | 64,036 | 54 ms | 3.9 MB | 0.6 ms |
| 8 | 270 | 575,787 | 582 ms | 34.8 MB | 0.6 ms |
| 9 | 283 | 1,727,239 | 2,031 ms | 104.4 MB | 0.6 ms |
| 10 | 296 | over 4,000,000 | gave up at the budget | — | 0.7 ms |

The diff's own peak allocation stays at 0.02 MB across every row.

Nine levels of nesting is 283 nodes in the file and 1.7 million after
expansion. The diff does not move, because it compares pointer pairs and
memoises on `(old pointer, new pointer, variance)`: a component referenced from
twenty responses is compared once per side, and a recursive schema terminates
the first time it meets itself instead of unrolling. Cost is linear in the
number of distinct reachable pointer pairs.

Absolute timings will not reproduce on your machine and are not the point. Run
`python bench/bench_diff.py --out bench/results-scaling.csv` and look at the
shape of the two curves.

The description in this repository is shallow, so inlining it costs 2.8x and
five milliseconds. That is the honest caveat: this only matters once envelopes
nest, which they do as soon as an API has a paginated list of resources that
contain sub-resources. `inline_refs(document)` is still exported, with a node
budget, because it is the right tool for producing a bundled artefact. It is
the wrong one for comparing two of them.

## What breaks, and what happens instead

The traversal has two ceilings and neither of them raises. `max_nodes` stops the
walk and sets `truncated`; `max_changes` stops collecting and records how many
were dropped. A diff that comes back partial is more useful in a pull request
comment than a stack trace, and a CI job whose only job is to say something
about a change should not fall over on the one change big enough to matter.

Cosmetic differences are excluded unless you ask for them. On a description that
is actively maintained they are most of the diff, and a report where the one
breaking change sits below thirty rewritten descriptions is a report that gets
collapsed and never expanded again. `--include-cosmetic` when you want them.

External `$ref` is refused rather than fetched. A diff that reaches across the
network gives a different answer depending on where it runs, which makes it
useless as a gate. Bundle first.

Lint rules are isolated from each other. A rule that raises is recorded in
`crashed` and the run continues with the rest, because the point of a linter is
to be pointed at descriptions you did not write, and those break assumptions.

Exit codes separate the two failures a pipeline needs to tell apart: 1 means the
API changed, 2 means the check itself could not run — a file that will not load,
an external ref, a waiver with no reason. Collapsing them is how a gate ends up
passing for three weeks because somebody renamed a path.

## Things that looked right and were not

**Annotating components with their variance.** The first version marked each
component as input or output and looked the flag up. It does not work: variance
belongs to the route, not the schema, and in this description `Source`,
`ColorName`, `Orientation` and `LicenseType` are all reached both ways. There is
no flag to set. `reachability(document)` computes the three groups with a
worklist over `(component, variance)` pairs, and `pexafy-openapi reach` prints
them.

**Comparing `type` as written.** OpenAPI 3.0 spells nullability `nullable:
true`; 3.1 spells it `type: ["string", "null"]`. A description that moved from
3.0 to 3.1 changes every nullable field, and a differ that compares the `type`
key reports a breaking type change on all of them. That was a hundred and forty
findings on a migration with no contract change in it at all. Type and
nullability are pulled apart before comparison.

**Comparing security requirement lists as sequences.** The list is a set of
alternatives, so an entry appearing is additive and an entry disappearing is
what breaks callers. Treating it positionally reports the wrong half. Comparing
only the requirement lists misses the case where a scheme keeps its name and
changes from a bearer token to an `X-Api-Key` header: the lists are identical
and every caller starts getting a 401, so the schemes themselves are compared
too.

**Deciding whether two `oneOf` unions describe the same set.** Abandoned.
Members are compared by index and a change in arity is reported as notable so a
human looks. Reordering a `oneOf` therefore produces noise. That is the honest
trade against claiming a break that is not there, and it is written down in the
docstring rather than hidden.

**Reporting every closed enum in the document.** A closed set on a filter is
correct and flagging it is pure noise, so `response-enum-extensible` only fires
on components reachable from a response. Working that out needs the reachability
pass; a naming convention does not survive contact with a real description.

## Waivers

Sooner or later a breaking change is the right call. A gate that can only say no
gets switched off, or the baseline gets edited until the finding disappears,
which loses the fact that anybody decided anything.

```json
{
  "waivers": [
    {
      "code": "enum.value.added",
      "location": "/components/schemas/Source",
      "reason": "Wikimedia Commons was indexed in 1.3.0, so Source gained a value.",
      "expires": "2027-08-01"
    }
  ]
}
```

A reason is required. A waiver without one is a mute button, so the file is
refused outright. `location` is a glob over the JSON Pointer, so a code can be waived on
one endpoint without being waived across the API. Expiry is the part that
matters: without it the file becomes a graveyard and a two-year-old entry keeps
the gate quiet about an edit somewhere else entirely. An expired waiver is
reported rather than silently dropped, and a waiver that matched nothing is
reported too, because it is a claim about a decision that no longer applies.

```bash
pexafy-openapi check openapi/v1.json \
  --baseline openapi/releases/1.2.0.json \
  --waivers waivers.json
```

`apply_waivers(report, waivers)` is the same thing from Python.

## The rest of the commands

`pexafy-openapi lint` runs the publishing rules against one description: missing
or duplicate `operationId`, untyped success responses, operations that document
no failure, closed response enums, `additionalProperties: false` on a request
body, mixed pagination styles, unreferenced components, and server URLs pointing
somewhere that is not the public API. `lint(document)` from Python.

`pexafy-openapi reach` groups components by which side they are reached from.
`pexafy-openapi rules` prints the classification table. `pexafy-openapi stats`
prints the shape of a document.

## Limitations

Only OpenAPI 3.x. Swagger 2.0 is refused with a message telling you to convert
first, rather than half-understood.

`allOf`, `oneOf` and `anyOf` are compared positionally, so a reordering shows up
as noise. `not` is not interpreted. Regular expression containment is not
decided, so any change to `pattern` is reported as a tightening — over-reporting
a break costs a waiver, under-reporting one costs an incident.

Codes the table does not know are reported as notable rather than dropped.
Silence about a change nobody classified is what makes people stop trusting the
output.

## Licence

MIT.
