# tests/test_staging.py
"""The staged pipeline: step types, selection policy, access, read, handle, reporting.

Nothing here writes to the store; index-level behavior lives in test_index.py.
"""
import hashlib
import os
import sqlite3

from rtfm import config, extract, manifest, staging


def _reached(*paths):
    """Step 1's product for a test that starts at step 2 or 3A: access() records the mtime
    it stats while scanning, so nothing downstream stats again (ADR 0015).

    Falls back to 0.0 for a path that isn't on disk — step 2 is policy over names and never
    looks at content or mtime, so its tests name files they never create."""
    return [staging.Reached(p, p.stat().st_mtime if p.exists() else 0.0) for p in paths]


def _selected(src, root=None):
    """The names step 2 kept, for a test that cares about selection policy only."""
    return [r.path.name for r in staging.scan(src, root or src.path).wanted]


def _src(tmp_path, **kw):
    kw.setdefault("ext_blocklist", frozenset())
    return manifest.Source(name="s", type="dir", path=tmp_path, **kw)


def _empty_store():
    """The two store facts report_unsearchable reads; no index module needed."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        "CREATE TABLE contents (sha256 TEXT, error TEXT, extracted_ok INTEGER);"
        "CREATE TABLE locations (source TEXT, relpath TEXT, sha256 TEXT);"
    )
    return conn


def _indexed(src, root):
    """A pipeline result for a reporting test: steps 1-2 ran, nothing was read or handled."""
    scan = staging.scan(src, root)
    return staging.Indexed(
        reachable=scan.reachable,
        wanted=scan.wanted,
        read=staging.StepResult([], []),
        handled=staging.StepResult([], []),
        cache=staging.CacheStats(0, 0, 0, 0),
    )


def test_step_result_classifies_its_own_outcome():
    clean = staging.StepResult(kept=[1, 2], problems=[])
    partial = staging.StepResult(
        kept=[1], problems=[staging.Problem("a.md", "nope")]
    )
    empty = staging.StepResult(
        kept=[], problems=[staging.Problem("a.md", "nope")]
    )
    assert (staging.is_clean(clean), staging.is_partial(clean), staging.is_empty(clean)) == (
        True, False, False)
    assert (staging.is_clean(partial), staging.is_partial(partial),
            staging.is_empty(partial)) == (False, True, False)
    assert (staging.is_clean(empty), staging.is_partial(empty), staging.is_empty(empty)) == (
        False, False, True)


def test_a_problem_names_a_position_not_a_hash():
    p = staging.Problem("docs/a.md", "Permission denied")
    assert p.position == "docs/a.md"
    assert p._fields == ("position", "reason")


def test_select_returns_a_bare_list_not_a_step_result(tmp_path):
    # Step 2 asks about intent, where rtfm has no standing to call 90% filtered a partial
    # success. A bare list means there is nowhere to record a step-2 partial outcome.
    t = tmp_path / "c"
    t.mkdir()
    (t / "a.md").write_text("a")
    got = staging.select(_reached(t / "a.md"), t, _src(t))
    assert isinstance(got, list)
    assert not isinstance(got, staging.StepResult)


def test_select_keeps_supported_and_drops_the_rest(tmp_path):
    t = tmp_path / "c"
    t.mkdir()
    paths = _reached(t / "a.md", t / "b.png", t / "c.pdf", t / "d.rst")
    got = staging.select(paths, t, _src(t))
    assert sorted(r.path.name for r in got) == ["a.md", "c.pdf", "d.rst"]


def test_allowlist_selects_only_those_types(home, tmp_path):
    for n in ("a.md", "b.bzl", "c.png", "d.txt"):
        (tmp_path / n).write_text("x")
    src = manifest.Source(name="s", type="dir", path=tmp_path,
                      ext_allowlist=frozenset({".md", ".bzl"}))
    got = set(_selected(src))
    assert got == {"a.md", "b.bzl"}


def test_blocklist_selects_everything_but_the_union_with_rtfms_defaults(home, tmp_path):
    for n in ("a.md", "b.py", "c.png", "d.log"):
        (tmp_path / n).write_text("x")
    src = _src(tmp_path, ext_blocklist=frozenset({".log"}))
    got = set(_selected(src))
    assert got == {"a.md", "b.py"}          # .png from the default set, .log from the user's


def test_an_allowlist_never_consults_the_default_blocklist(home, tmp_path):
    (tmp_path / "a.png").write_text("x")
    src = manifest.Source(name="s", type="dir", path=tmp_path,
                      ext_allowlist=frozenset({".png"}))
    assert _selected(src) == ["a.png"]


def test_paths_scopes_to_a_prefix(home, tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "src").mkdir()
    (tmp_path / "docs" / "a.md").write_text("x")
    (tmp_path / "src" / "b.md").write_text("x")
    src = _src(tmp_path, paths=("docs",))
    assert _selected(src) == ["a.md"]


def test_an_exclusion_beats_an_inclusion(home, tmp_path):
    (tmp_path / "docs" / "versions").mkdir(parents=True)
    (tmp_path / "docs" / "a.md").write_text("x")
    (tmp_path / "docs" / "versions" / "old.md").write_text("x")
    src = _src(tmp_path, paths=("docs",), exclude_paths=("docs/versions",))
    assert _selected(src) == ["a.md"]


def test_an_exclusion_alone_scopes_the_whole_tree_minus_that_prefix(home, tmp_path):
    (tmp_path / "keep").mkdir()
    (tmp_path / "drop").mkdir()
    (tmp_path / "keep" / "a.md").write_text("x")
    (tmp_path / "drop" / "b.md").write_text("x")
    src = _src(tmp_path, exclude_paths=("drop",))
    assert _selected(src) == ["a.md"]


def test_a_prefix_does_not_match_a_partial_directory_name(home, tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docsets").mkdir()
    (tmp_path / "docs" / "a.md").write_text("x")
    (tmp_path / "docsets" / "b.md").write_text("x")
    src = _src(tmp_path, paths=("docs",))
    assert _selected(src) == ["a.md"]


def test_an_exclusion_that_is_a_prefix_of_an_inclusion_selects_nothing(home, tmp_path):
    # paths=("docs/api",) narrows to a subtree that exclude_paths=("docs",) then excludes
    # wholesale — exclusion wins unconditionally (ADR 0014), so this is the "looks correct,
    # indexes nothing" shape the ADR warns about, not a bug in either clause alone.
    (tmp_path / "docs" / "api").mkdir(parents=True)
    (tmp_path / "docs" / "api" / "a.md").write_text("x")
    src = _src(tmp_path, paths=("docs/api",), exclude_paths=("docs",))
    assert _selected(src) == []


def test_a_root_level_file_is_not_selected_under_a_restrictive_paths(home, tmp_path):
    # readme.md sits at the source root, outside the "docs" prefix — paths narrows to a
    # subtree, it never implicitly keeps the root alongside it.
    (tmp_path / "readme.md").write_text("x")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "a.md").write_text("x")
    src = _src(tmp_path, paths=("docs",))
    assert _selected(src) == ["a.md"]


def test_a_dotdot_paths_entry_selects_nothing(home, tmp_path):
    # Task 1 rejects glob characters in `paths` but not a `..` segment. What's actually
    # guaranteed: selection can never leave the source root, because the prefix match is
    # against a relpath _rel produces relative to `base`, and access() only ever enumerates
    # beneath that root — a relpath can neither render as absolute nor climb above it, so a
    # ".." entry can't match anything.
    (tmp_path / "a.md").write_text("x")
    assert _selected(_src(tmp_path, paths=("..",))) == []


def test_a_leading_slash_in_paths_normalizes_to_a_root_anchored_prefix(home, tmp_path):
    # A leading "/" in a manifest's `paths` entry is root-anchoring, the same convention
    # .gitignore uses — _normalize_paths strips it, so "/docs" becomes the source-relative
    # prefix "docs" and DOES select the docs/ subtree. Going through _source_from_table (the
    # real manifest parsing path), not _src: _src builds a Source directly and bypasses
    # _normalize_paths, which is why an earlier version of this test could (wrongly) claim
    # a leading-"/" entry matches nothing.
    (tmp_path / "docs").mkdir()
    (tmp_path / "etc").mkdir()
    (tmp_path / "docs" / "a.md").write_text("x")
    (tmp_path / "etc" / "passwd.md").write_text("x")
    src = manifest.source_from_table({
        "name": "s", "type": "dir", "path": str(tmp_path),
        "paths": ["/docs"], "ext_blocklist": [],
    })
    assert src.paths == ("docs",)
    assert _selected(src) == ["a.md"]


def test_access_returns_reachable_positions(tmp_path):
    t = tmp_path / "c"
    (t / "sub").mkdir(parents=True)
    (t / "a.md").write_text("a")
    (t / "sub" / "b.md").write_text("b")
    s = manifest.Source(name="s", type="dir", path=t)
    got = staging.access(s)
    assert sorted(r.path.name for r in got.kept) == ["a.md", "b.md"]
    assert all(r.mtime == r.path.stat().st_mtime for r in got.kept)   # step 1 carries freshness
    assert got.problems == []
    assert staging.is_clean(got)


def test_access_names_a_directory_it_cannot_enter(tmp_path, unopenable):
    t = tmp_path / "c"
    t.mkdir()
    (t / "a.md").write_text("a")
    unopenable(t / "vault", directory=True)
    got = staging.access(manifest.Source(name="s", type="dir", path=t))
    # rglob swallows this inside scandir and yields nothing, which is indistinguishable from
    # an empty directory — so the subtree was invisible to every code path.
    assert staging.is_partial(got)
    assert any("vault" in p.position for p in got.problems)


def test_access_names_a_file_it_cannot_open(tmp_path, unopenable):
    t = tmp_path / "c"
    t.mkdir()
    (t / "a.md").write_text("a")
    unopenable(t / "locked.md")
    got = staging.access(manifest.Source(name="s", type="dir", path=t))
    assert any(p.position == "locked.md" for p in got.problems)
    assert [r.path.name for r in got.kept] == ["a.md"]


def test_access_reports_unfiltered(tmp_path, unopenable):
    # We cannot filter what we could not read — an inaccessible directory might hold exactly
    # the files the user wants, and its contents are unknowable from outside (ADR 0015).
    t = tmp_path / "c"
    t.mkdir()
    (t / "a.md").write_text("a")
    unopenable(t / "image.png")
    got = staging.access(manifest.Source(name="s", type="dir", path=t))
    assert any(p.position == "image.png" for p in got.problems)


def test_read_hashes_what_it_can(tmp_path):
    t = tmp_path / "c"
    t.mkdir()
    (t / "a.md").write_text("alpha")
    got = staging.read_bytes_for(_reached(t / "a.md"), t, {})
    assert len(got.kept) == 1
    path, sha, mtime = got.kept[0]
    assert path.name == "a.md" and len(sha) == 64
    assert got.problems == []


def test_read_names_a_file_deleted_since_the_scan(tmp_path):
    # Step 1 stats, step 3A reads, and a file can vanish in between — the race ADR 0015 gives
    # step 3A. A cache hit must not skip the check: reusing the stored sha for a file that is
    # gone keeps its row and reports nothing, and the index then serves a path that 404s.
    t = tmp_path / "c"
    t.mkdir()
    f = t / "a.md"
    f.write_text("alpha")
    scanned = _reached(f)                       # step 1 saw it
    sha = staging.read_bytes_for(scanned, t, {}).kept[0].sha
    f.unlink()                                  # ...and it goes before step 3A gets there
    got = staging.read_bytes_for(scanned, t, {"a.md": (sha, scanned[0].mtime)})
    assert got.kept == []
    assert [p.position for p in got.problems] == ["a.md"]


def test_read_keeps_the_scans_mtime_against_new_content(tmp_path):
    # The mtime stored is step 1's, not a fresh one. A file edited between the scan and the
    # read is hashed as it is now, but recorded with the older mtime, so the next run sees a
    # mismatch and re-reads once. The reverse — a newer mtime against older bytes — would
    # make the stale content read as fresh forever.
    t = tmp_path / "c"
    t.mkdir()
    f = t / "a.md"
    f.write_text("alpha")
    scanned = _reached(f)
    f.write_text("beta")                        # changed after the scan...
    later = scanned[0].mtime + 10
    os.utime(f, (later, later))                 # ...landing with a later mtime, deterministically
    got = staging.read_bytes_for(scanned, t, {}).kept[0]
    assert got.sha == hashlib.sha256(b"beta").hexdigest()      # hashed as it is now
    assert got.mtime == scanned[0].mtime                       # recorded as it was scanned
    assert got.mtime < f.stat().st_mtime                       # i.e. older, never newer


def test_read_names_a_file_whose_bytes_it_cannot_get(tmp_path, unopenable):
    t = tmp_path / "c"
    t.mkdir()
    (t / "a.md").write_text("alpha")
    unopenable(t / "locked.md")
    got = staging.read_bytes_for(_reached(t / "a.md", t / "locked.md"), t, {})
    # One unreadable file used to raise straight out of the reindex.
    assert [p.position for p in got.problems] == ["locked.md"]
    assert len(got.kept) == 1


def test_read_reuses_a_cached_hash(tmp_path):
    t = tmp_path / "c"
    t.mkdir()
    f = t / "a.md"
    f.write_text("alpha")
    mtime = f.stat().st_mtime
    got = staging.read_bytes_for(_reached(f), t, {"a.md": ("cafebabe", mtime)})
    assert got.kept[0][1] == "cafebabe"      # no re-hash when the key matches


def test_handle_fans_a_failed_content_out_to_every_position(home, tmp_path):
    # Three byte-identical corrupt files are one extraction and three broken files. Reporting
    # the sha would make every consumer convert contents into files — the conversion that
    # printed a content count as "N file(s)".
    t = tmp_path / "c"
    t.mkdir()
    bad = b"%PDF-1.4 not really a pdf" + bytes(range(256))
    for n in ("v1.pdf", "v2.pdf", "v3.pdf"):
        (t / n).write_bytes(bad)
    read = staging.read_bytes_for(_reached(t / "v1.pdf", t / "v2.pdf", t / "v3.pdf"), t, {})
    sha = read.kept[0][1]
    got = staging.handle(read.kept, {sha}, t)
    assert sorted(p.position for p in got.problems) == ["v1.pdf", "v2.pdf", "v3.pdf"]
    assert all("pdf" not in p.reason.lower() or "utf-8" not in p.reason.lower()
               for p in got.problems)          # reported in the handler's own terms


def test_nothing_selected_names_the_reachable_count_and_the_policy(home, tmp_path):
    # A manifest whose extension list matches nothing indexes zero files and, before this,
    # gave no reason (ADR 0015's opening example).
    d = tmp_path / "c"
    d.mkdir()
    for n in ("a.md", "b.md", "c.md"):
        (d / n).write_text("x")
    src = manifest.Source(name="bazel", type="dir", path=d, ext_allowlist=frozenset({".nomatch"}))
    conn = _empty_store()
    result = _indexed(src, d)
    [msg] = staging.report_all(conn, src, result)
    assert "NOTHING SELECTED 'bazel'" in msg
    assert "3 path(s) reachable" in msg
    assert "ext_allowlist = .nomatch" in msg


def test_nothing_selected_names_the_paths_policy_too(home, tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "a.md").write_text("x")
    src = manifest.Source(name="s", type="dir", path=tmp_path, paths=("nope",),
                      ext_blocklist=frozenset())
    conn = _empty_store()
    result = _indexed(src, tmp_path)
    [msg] = staging.report_all(conn, src, result)
    assert "paths = nope" in msg


def test_nothing_selected_in_blocklist_mode_summarizes_defaults_by_count(home, tmp_path):
    # The effective blocklist is the user's list unioned with rtfm's ~70 defaults. Dumping
    # the whole union produced a 700-character wall the user could not act on and that
    # buried the `paths` clause (fix round 1: a real defect, not a style choice). Name what
    # the user wrote; summarize the rest by count, and never show a default extension the
    # user never declared.
    d = tmp_path / "c"
    d.mkdir()
    (d / "a.png").write_bytes(b"x")          # blocked by rtfm's default set, not by the user
    src = manifest.Source(name="s", type="dir", path=d, ext_blocklist=frozenset({".md"}))
    conn = _empty_store()
    result = _indexed(src, d)
    [msg] = staging.report_all(conn, src, result)
    assert "ext_blocklist = .md" in msg
    assert f"rtfm's {len(config.DEFAULT_EXT_BLOCKLIST)} default" in msg
    assert ".png" not in msg                 # the user never wrote it; the wall is gone


def test_nothing_selected_in_blocklist_mode_with_an_empty_user_list(home, tmp_path):
    d = tmp_path / "c"
    d.mkdir()
    (d / "a.png").write_bytes(b"x")
    src = manifest.Source(name="s", type="dir", path=d, ext_blocklist=frozenset())
    conn = _empty_store()
    result = _indexed(src, d)
    [msg] = staging.report_all(conn, src, result)
    assert "ext_blocklist = []" in msg
    assert f"rtfm's {len(config.DEFAULT_EXT_BLOCKLIST)} default" in msg
    assert ".png" not in msg


def test_an_empty_source_directory_selects_silently(home, tmp_path):
    # An empty folder is not a manifest error — reachable.kept is empty too, which is step
    # 1's story (silence), never step 2's.
    d = tmp_path / "c"
    d.mkdir()
    src = manifest.Source(name="s", type="dir", path=d, ext_allowlist=frozenset({".md"}))
    conn = _empty_store()
    result = _indexed(src, d)
    assert staging.report_all(conn, src, result) == []


def test_an_unreadable_root_reports_access_only_not_nothing_selected(home, tmp_path, unopenable):
    t = tmp_path / "c"
    unopenable(t, directory=True)
    src = manifest.Source(name="s", type="dir", path=t, ext_allowlist=frozenset({".md"}))
    conn = _empty_store()
    result = _indexed(src, t)
    msgs = staging.report_all(conn, src, result)
    assert any("COULD NOT OPEN" in m for m in msgs)
    assert not any("NOTHING SELECTED" in m for m in msgs)


def test_a_read_failure_does_not_tell_you_to_edit_the_manifest(home):
    result = staging.StepResult([], [staging.Problem("a.md", "Permission denied")])
    [msg] = staging.report("s", result, "read")
    assert "storage" in msg
    assert str(config.manifest_path()) not in msg


def test_an_access_failure_does_name_the_manifest(home):
    result = staging.StepResult([], [staging.Problem("vault", "Permission denied")])
    [msg] = staging.report("s", result, "access")
    assert str(config.manifest_path()) in msg


def test_report_marks_elided_positions_and_reasons(home):
    problems = [staging.Problem(f"f{i}.md", f"reason {i}") for i in range(5)]
    [msg] = staging.report("s", staging.StepResult([], problems), "handle")
    assert "(+2 more)" in msg          # 5 positions, 3 shown
    assert "(+3 more)" in msg          # 5 distinct reasons, 2 shown


def test_an_allowlisted_bzl_file_is_selected_and_read_as_text(home, tmp_path):
    # The whole point of ADR 0014: a .bzl file is selected by the manifest and handled by
    # the plain-text routine. TEXT_EXTS deciding both is the defect being deleted.
    assert not hasattr(config, "TEXT_EXTS")
    assert not hasattr(extract, "TEXT_EXTS")
    (tmp_path / "r.bzl").write_text("def cc_shared_library(shared_lib_name): pass")
    src = manifest.Source(name="s", type="dir", path=tmp_path,
                          ext_allowlist=frozenset({".bzl"}))
    assert _selected(src) == ["r.bzl"]
    rows = extract.rows_for_file(tmp_path / "r.bzl")
    assert rows and rows[0][0] == "line"
