from __future__ import annotations

import zipfile

from devicedock.previews import ARCHIVE_MEMBER_LIMIT, TEXT_LIMIT, prepare_preview


def test_text_preview_is_bounded_and_readable(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_text("a" * (TEXT_LIMIT + 10))
    preview = prepare_preview(path)
    assert preview.kind == "text"
    assert len(preview.text) < TEXT_LIMIT + 100
    assert "Preview truncated" in preview.text


def test_unknown_binary_preview_never_renders_hex(tmp_path):
    path = tmp_path / "payload.bin"
    path.write_bytes(b"\x00\xffprivate payload")
    preview = prepare_preview(path)
    assert preview.kind == "unsupported"
    assert "Export" in preview.message
    assert "private payload" not in preview.text


def test_oversized_docx_xml_is_not_decompressed_for_preview(tmp_path):
    path = tmp_path / "large.docx"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", "x" * (ARCHIVE_MEMBER_LIMIT + 1))
    preview = prepare_preview(path)
    assert preview.kind == "text"
    assert "too large" in preview.text
