
import pytest


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Point RTFM_HOME at a tmp dir for the duration of a test."""
    h = tmp_path / "rtfmhome"
    h.mkdir()
    monkeypatch.setenv("RTFM_HOME", str(h))
    return h


@pytest.fixture
def sample_pdf(tmp_path):
    """A 2-page PDF with known text, written via pymupdf."""
    import fitz
    doc = fitz.open()
    p1 = doc.new_page()
    p1.insert_text((72, 72), "alpha bravo charlie on page one")
    p2 = doc.new_page()
    p2.insert_text((72, 72), "delta echo foxtrot on page two")
    path = tmp_path / "sample.pdf"
    doc.save(str(path))
    doc.close()
    return path


@pytest.fixture
def sample_txt(tmp_path):
    path = tmp_path / "notes.md"
    path.write_text("\n".join(f"line {i} keyword{i}" for i in range(1, 121)))
    return path
