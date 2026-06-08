# tests/test_e2e.py
import rtfm_server as rt


def test_zero_config_drop_and_search(home, sample_pdf):
    # 1) fresh home: bootstrap default source by loading the manifest
    rt.load_manifest()
    # 2) user drops a PDF into the default source
    dest = rt.default_source_dir() / "spec.pdf"
    dest.write_bytes(sample_pdf.read_bytes())
    # 3) search finds it with a page locator
    out = rt.search(query="foxtrot")
    hit = next(h for h in out["results"] if h["relpath"] == "spec.pdf")
    assert hit["locator_kind"] == "page" and hit["locator_value"] == "2"
    # 4) read the page back
    text = rt.read(source="default", relpath="spec.pdf", start=2, end=2)
    assert "foxtrot" in text
