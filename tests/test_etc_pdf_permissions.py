from pathlib import Path
from tempfile import TemporaryDirectory

from app.etc_accounting.fetcher import _stored_pdf_is_readable


def test_stored_pdf_readability_requires_a_real_pdf():
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        valid = root / "valid.pdf"
        invalid = root / "invalid.pdf"
        valid.write_bytes(b"%PDF-1.7\n")
        invalid.write_bytes(b"not a pdf")
        assert _stored_pdf_is_readable(valid)
        assert not _stored_pdf_is_readable(invalid)
        assert not _stored_pdf_is_readable(root / "missing.pdf")
