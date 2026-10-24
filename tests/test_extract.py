# tests/test_extract.py
"""Extraction routines: the registry, locator shapes, and the worker pool.

The pipeline-level cases that assert index state live in test_index.py; these
tests exercise extract's own public surface.
"""
import os

from rtfm import extract


def test_one_registry_drives_body_and_signal():
    # These dispatched independently and could disagree about a file — a .bzl was
    # selected by one and ignored by the other, so it indexed to zero rows.
    assert extract._routine_for(".pdf").name == "pdf"
    assert extract._routine_for(".md").name == "markup"
    assert extract._routine_for(".bzl").name == "text"
    assert extract._routine_for("").name == "text"


def test_the_fallback_is_not_a_member_of_the_ordered_registry():
    # Inside the tuple it would match unconditionally, shadowing anything after it.
    assert extract.DEFAULT_ROUTINE not in extract.ROUTINES
    assert all(r.exts for r in extract.ROUTINES)


def test_markup_routine_still_extracts_headings(tmp_path):
    f = tmp_path / "x.md"
    f.write_text("# Title\n\nbody text\n\n## Section\n")
    title, headings = extract.doc_signal_for_file(f)
    assert title == "Title" and "Section" in headings


def test_first_substantial_line_skips_short_and_numeric():
    assert extract._first_substantial_line("1234\nabc\nReal Title\nmore") == "Real Title"


def test_pdf_rows_carry_page_locators(sample_pdf):
    rows = extract.rows_for_file(sample_pdf)
    assert [r[0] for r in rows] == ["page", "page"]
    assert [r[1] for r in rows] == ["1", "2"]
    assert "alpha bravo charlie" in rows[0][2]


def test_markup_rows_chunk_at_fifty_lines(tmp_path):
    f = tmp_path / "long.md"
    f.write_text("\n".join(f"line {i}" for i in range(60)) + "\n")
    rows = extract.rows_for_file(f)
    assert [r[1] for r in rows] == ["1", "51"]
    assert rows[0][2].splitlines()[0] == "line 0"
    assert rows[1][2].splitlines()[0] == "line 50"


def test_unknown_extension_keeps_line_locators_and_no_signal(tmp_path):
    f = tmp_path / "x.bzl"
    f.write_text("# licence header\nsome content\n")
    rows = extract.rows_for_file(f)
    assert rows and rows[0][0] == "line"
    assert extract.doc_signal_for_file(f) == ("", "")


def test_failed_extraction_returns_an_error_instead_of_raising(tmp_path):
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"\x00\x01\x02\x03 this is definitely not a pdf and has no pdf header")
    rows, title, headings, error = extract.extract_rows(str(bad))
    assert (rows, title, headings) == ([], "", "")
    assert error


def test_extract_many_returns_one_result_per_job(tmp_path):
    jobs = []
    for i in range(3):
        f = tmp_path / f"f{i}.md"
        f.write_text(f"# Title {i}\nbody {i}\n")
        jobs.append((f"sha{i}", str(f)))
    out = extract.extract_many(jobs)
    assert {e.sha for e in out} == {"sha0", "sha1", "sha2"}
    assert {e.title for e in out} == {"Title 0", "Title 1", "Title 2"}


def test_extract_many_runs_workers_in_this_process(tmp_path, monkeypatch):
    """Regression guard for the FastMCP-server deadlock: parallel extraction must run in THIS
    process (a thread pool), never a forked child. A process pool deadlocks inside the server
    (forked workers inherit thread-held locks and hang). Force the pool path and assert every
    extraction ran under our PID."""
    jobs = []
    for i in range(4):
        f = tmp_path / f"f{i}.md"
        f.write_text(f"body {i} keyword{i}\n")
        jobs.append((f"sha{i}", str(f)))
    monkeypatch.setenv("RTFM_WORKERS", "2")

    seen_pids: list[int] = []
    real = extract.extract_rows

    def spy(path_str):
        seen_pids.append(os.getpid())
        return real(path_str)

    monkeypatch.setattr(extract, "extract_rows", spy)
    out = extract.extract_many(jobs)

    assert len(out) == 4
    assert seen_pids, "extraction worker never ran in this process (out-of-process pool?)"
    assert all(pid == os.getpid() for pid in seen_pids)


def test_workers_respects_env(monkeypatch):
    monkeypatch.setenv("RTFM_WORKERS", "3")
    assert extract.workers() == 3
    monkeypatch.delenv("RTFM_WORKERS", raising=False)
    assert extract.workers() >= 1
