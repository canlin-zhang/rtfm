---
status: accepted
---

# Per-source scope: `paths` prefixes, and extension lists that replace the hardcoded default

A Source indexes its whole tree, and which files count as text is a constant in
rtfm's source (`TEXT_EXTS`). Two needs broke that (rtfm#24): documentation that
lives in a format outside the set, and a repo where only one subtree is wanted.

The concrete case: Bazel's Starlark `doc=` strings are the canonical rule
reference — `bazelbuild/bazel` no longer carries the C++ rules at all — so a
`rules_cc` source indexes 26 files and answers no question about a rule's
attributes. It surfaced in a code review needing `cc_shared_library`'s
`shared_lib_name`, which lives in `cc/private/rules_impl/cc_shared_library.bzl`.

## Measurements

Taken against a clean full-tree clone of `bazelbuild/bazel` at `master`
(37e3e8c6dc) and `bazelbuild/rules_cc` at `main`.

| Quantity | Value |
| --- | --- |
| bazel: tracked files | 13,271 |
| bazel: files matching the old default set | 4,060 |
| bazel: those under `docs/` | 3,919 (96.5%) |
| bazel: those under `docs/versions/` | 3,759 — 96% of the `docs/` total |
| bazel: current docs, i.e. `docs/` minus `versions/` | 160 |
| bazel: repo size | ~1.0 GB (GitHub API) |
| rules_cc: `.bzl` files | 248, of which 81 under `tests/` |
| rules_cc: repo size | ~2.2 MB |

An earlier draft of this ADR reported that `docs/versions/` did not exist and
that a full bazel clone was 20 MB. Both were measured against `~/.rtfm/repos/bazel`,
which is a `blob:none` partial clone with `core.sparseCheckout` set to `/docs/**`
and `!/docs/versions/**` — the out-of-band workaround rtfm#24 describes. The
numbers above come from an unfiltered clone.

## Decisions

### `paths`: directory prefixes, include and exclude, no globs

```toml
paths = ["docs", "!docs/versions"]
```

A bare entry includes a prefix; a `!` entry excludes one. Unset means the whole
tree. Prefixes only — `*`, `?` and `[` are rejected as config errors, because a
literal `docs/**` directory does not exist and would index nothing while looking
correct.

Exclusion is not optional. `docs/versions/` holds 12 archived doc sets and 96% of
bazel's indexable `docs/` files; without it, scoping to `docs` buys 160 wanted
files and 3,759 historical copies competing with them — the stale-content failure
[ADR 0013](0013-git-repo-source-type.md) exists to prevent, answers cited from
7.6.1 when the question was about 9.1.0. Expressing that with include-prefixes
alone means listing 28 siblings of `versions/`.

Glob *wildcards* remain rejected: no measured case needs one, and a prefix list is
a forward-compatible subset of a glob language, so they stay addable later.

### Extension selection is declared per source; no hardcoded default set

`TEXT_EXTS` had been doing two unrelated jobs under one name — deciding which
files are selected, and deciding which files get markdown heading parsing. Only
the second is a handling routine. The selection half is deleted.

```toml
ext_allowlist = [".md", ".pdf", ".bzl"]      # only these types
ext_blocklist = [".png", ".svg", ".woff"]    # everything except these
```

Exactly one of the two is required. Declaring both is a config error and the
source is refused. Declaring neither skips the source with a message naming both
keys — rtfm does not guess what to index, and a guess is how the hidden default
returns through a side door.

A **default allowlist is unsafe and a default blocklist is not**, and the
asymmetry is the whole reason this shape works. An allowlist decides what you
*get*: growing it later means every existing source silently misses the new type.
That already happened — `.mdx` was added to `TEXT_EXTS` in `d71f905`, shipped in
0.6.1, because bazel.build's docs are `.mdx` and nothing else; any source pinned
to an older set would index zero files from a tree it was correctly pointed at
and report success. A blocklist decides what you *don't* get, and everything on it
is something nobody wants indexed, so growing it later removes garbage, never
content.

So `ext_blocklist` mode consults `DEFAULT_EXT_BLOCKLIST`, a set of known binary
and asset extensions held in rtfm's source, unioned with the user's list.
`ext_allowlist` mode never consults it: "only these types" already excludes
everything else.

### File filtering and encoding support are separate concerns

Recorded in full as [ADR 0015](0015-indexing-is-a-staged-pipeline.md); in brief, they
answer different questions and must not substitute for each other.

- **Filtering** — "should a file of this kind be indexed at all?" Answered
  exclusively by the extension lists above. No content inspection, ever. A file
  outside the lists was never a candidate; that is not an error and nothing is
  reported.
- **Encoding** — "can rtfm read a file it already agreed to index?" rtfm supports
  UTF-8. A file that does not decode is recorded as a failed extraction in the
  existing `contents.error` column, naming the file.

Using a failed decode as a filter would be the wrong mechanism reporting the wrong
cause: a `.png` that slipped past the lists would be reported as "not valid UTF-8"
when the true answer is that the blocklist is missing `.png`.

### Decoding is strict; `errors="replace"` is removed

The three decode sites (`_rows_for_file`, `_text_doc_signal`,
`read_document_text`) used `errors="replace"`, which forces any byte sequence to
become text by substituting U+FFFD. That is what let a binary file index cleanly
with `errors: 0`. Strict decoding needs no sniffing heuristic, no `libmagic`, and
no new dependency: a file that decodes is a text serialization and one that does
not, is not.

Cost: a genuinely-text file in cp1252 or UTF-16 now fails loudly instead of being
silently mangled into the index. If real non-UTF-8 documents turn up, the fix is
attempting a second named encoding, never returning to replacement.

### Extraction errors must reach the tools

Once rtfm states a supported-format boundary, a user has to be able to hear that a
file hit it. `summary["errors"]` and `contents.error` are populated but no tool
surfaces them. `search`, `reindex` and `health_check` do.

### Markup heading parsing stays in code

`MARKUP_EXTS` — the set that gets ATX/setext heading extraction for doc-level
ranking ([ADR 0012](0012-doc-level-signal-ranking.md)) — remains a constant,
because it is a handling routine like the PDF branch, not a selection default. A
file selected by an extension outside it is body-searchable with no title/heading
signal.

### Both keys apply to `dir` sources, not only `git_repo`

Filtering is not a git concept. A `dir` source over a large tree has the same
need, and confining the keys to `git_repo` would re-split the source model on
content rather than sync method ([ADR 0002](0002-source-types-by-sync-method.md)).

### A gitlink in `paths` carries the submodule case — no separate key

rtfm#24 proposed a `submodules` key alongside `paths`. One list is enough.
Initialization is not implemented and no motivating repo has submodules —
`bazelbuild/bazel` has no `.gitmodules`.

### A manifest scope change makes a git_repo source stale

`git_repo` staleness is commit-based (ADR 0013), so editing any of the four keys
would leave a source reporting "up to date" while serving the old file set — the
confident-but-wrong failure ADR 0013 exists to kill. `source_meta` gains a
`config_scope` column holding the normalized `(paths, exclude_paths,
ext_allowlist, ext_blocklist)` tuple as a string. It is compared before every other branch,
including the SHA-pin short-circuit: a pin freezes the commit, not the manifest.

Stored as the value rather than a digest — order-independence comes from sorting,
not from hashing, and a readable column beats an opaque one when someone inspects
the index by hand.

`dir` sources need no stored scope: their relpath+mtime set comparison goes stale
whenever the selected file set changes. It does not cover one edge, though — a
source that selects *nothing* has `indexed == on_disk == {}`, which reads as fresh
forever, so an empty selection is forced stale explicitly. Without that, the
source is skipped on every query and its stage report is never even computed.

Amends ADR 0013 (commit-based staleness → commit-or-scope).

### Schema version 4 → 5

The index is a cache; every bump is a clean-slate rebuild.

## Consequences

- `TEXT_EXTS` is deleted. `MARKUP_EXTS` and `DEFAULT_EXT_BLOCKLIST` replace it,
  each doing one job.
- `Source` gains `paths`, `exclude_paths`, `ext_allowlist` and `ext_blocklist`,
  all normalized at manifest-parse time and immutable.
- `iter_source_files` takes the real `Source`, not the synthetic
  `Source(type="dir", path=root)` it built before — that stand-in discards the
  filters and silently indexes the whole tree.
- Every existing manifest must declare one extension list. At 0.7.0, with no
  forks, the migration cost is the loud skip message and one edited line.
- `manifest.example.toml` and the bootstrap manifest document all four keys;
  undocumented, they are invisible to anyone reading the manifest, which is what
  drove the sparse-checkout workaround in the first place.
