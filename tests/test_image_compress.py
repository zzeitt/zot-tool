"""Image-compression unit tests — pure logic, no Zotero API access.

Tests `_compress_image_bytes`, `_compress_images_in_html`, `_prep_upload_file`,
and `_fit_note_under_limit` from zot.py using Pillow-generated fixtures. These
functions are pure (bytes in → bytes out, or HTML in → HTML out) and require no
network or Zotero credentials.
"""

import base64
import io
import os
import re
import sys

import pytest
from PIL import Image


# ---------------------------------------------------------------------------
# Fixture: import zot.py with dummy env vars (no API calls)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def zot():
    """Import zot.py with minimal dummy env vars.

    zot.py's module-level code only needs these vars to exist; the Zotero
    client it constructs is lazy and makes no API calls until used. Our pure
    compression functions never touch the API.
    """
    scripts_dir = os.path.join(os.path.dirname(__file__), "..", "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)

    saved = {}
    overrides = {
        "ZOTERO_API_KEY": "dummy-key",
        "ZOTERO_LIBRARY_ID": "000000",
        "ZOTERO_FORBIDDEN_COLLECTION": "DUMMYFORBIDDEN",
        "ZOTERO_MISC_COLLECTION": "DUMMYMISC",
    }
    for k, v in overrides.items():
        saved[k] = os.environ.get(k)
        os.environ[k] = v

    import zot as zot_module
    yield zot_module

    for k, orig in saved.items():
        if orig is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = orig


# ---------------------------------------------------------------------------
# Test image generators (Pillow)
# ---------------------------------------------------------------------------

def _make_jpeg(width, height, quality=95):
    """Generate a JPEG with non-trivial content (not solid color)."""
    img = Image.new("RGB", (width, height), (255, 0, 0))
    pixels = img.load()
    for y in range(height):
        for x in range(width):
            # Deterministic gradient + banding gives realistic JPEG size
            pixels[x, y] = ((x * 7 + y * 3) % 256, (x * 5) % 256, (y * 11) % 256)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality)
    return buf.getvalue()


def _make_png(width, height):
    """Generate a PNG with non-trivial content."""
    img = Image.new("RGBA", (width, height), (0, 128, 255, 255))
    pixels = img.load()
    for y in range(height):
        for x in range(width):
            pixels[x, y] = ((x * 7) % 256, (y * 3) % 256, (x + y) % 256, 255)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _make_animated_gif():
    """Generate a multi-frame animated GIF."""
    frames = []
    for i in range(3):
        img = Image.new("P", (100, 100), i * 40)
        frames.append(img)
    buf = io.BytesIO()
    frames[0].save(buf, "GIF", save_all=True, append_images=frames[1:],
                   duration=100, loop=0)
    return buf.getvalue()


def _make_noisy_png(width, height):
    """Generate a noise PNG — high entropy, so lossless compression can't shrink it
    (models photographic screenshots that PNG can't compress below the note budget)."""
    import random
    rng = random.Random(1234)  # deterministic
    img = Image.new("RGB", (width, height))
    px = img.load()
    for y in range(height):
        for x in range(width):
            px[x, y] = (rng.getrandbits(8), rng.getrandbits(8), rng.getrandbits(8))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _b64_wrap(data, line_len=76):
    """Base64-encode then wrap at line_len chars with CRLF.

    Mirrors org-mode's inline-image output: Emacs ``base64-encode-region``
    wraps at 76 chars by default, so the exported ``data:image/...;base64,...``
    data URI contains CRLF line breaks. A regex that only matches
    ``[A-Za-z0-9+/=]+`` stops at the first break and can't decode the image.
    """
    b64 = base64.b64encode(data).decode("ascii")
    return "\r\n".join(b64[i:i + line_len] for i in range(0, len(b64), line_len))


# ---------------------------------------------------------------------------
# _compress_image_bytes
# ---------------------------------------------------------------------------

class TestCompressImageBytes:
    def test_jpeg_large_resized_and_smaller(self, zot):
        """3000×2000 JPEG → resized to ≤1920px and byte size shrinks."""
        data = _make_jpeg(3000, 2000)
        result = zot._compress_image_bytes(data)

        img = Image.open(io.BytesIO(result))
        assert img.format == "JPEG"
        assert max(img.size) <= 1920
        assert len(result) < len(data)

    def test_png_format_preserved(self, zot):
        """PNG stays PNG (never converted to JPEG), regardless of resize."""
        data = _make_png(800, 600)
        result = zot._compress_image_bytes(data)

        assert result[:8] == b"\x89PNG\r\n\x1a\n"  # PNG magic bytes
        img = Image.open(io.BytesIO(result))
        assert img.format == "PNG"

    def test_animated_gif_unchanged(self, zot):
        """Animated GIF is returned byte-for-byte (never flattened)."""
        data = _make_animated_gif()
        result = zot._compress_image_bytes(data)
        assert result == data

    def test_invalid_bytes_returns_original(self, zot):
        """Garbage input is returned unchanged rather than raising."""
        data = b"this is not an image"
        result = zot._compress_image_bytes(data)
        assert result == data


# ---------------------------------------------------------------------------
# _compress_images_in_html (shared string core)
# ---------------------------------------------------------------------------

class TestCompressImagesInHtml:
    def test_compresses_base64(self, zot):
        """HTML with a large base64 JPEG → compressed and smaller."""
        data = _make_jpeg(3000, 2000)
        b64 = base64.b64encode(data).decode("ascii")
        html = f'<div><img src="data:image/jpeg;base64,{b64}"></div>'

        new_html, changed = zot._compress_images_in_html(html)

        assert changed
        assert len(new_html) < len(html)
        m = re.search(r'data:image/jpeg;base64,([A-Za-z0-9+/=]+)', new_html)
        assert m, "expected compressed base64 data URI"
        new_img = Image.open(io.BytesIO(base64.b64decode(m.group(1))))
        assert max(new_img.size) <= 1920

    def test_no_images_unchanged(self, zot):
        """No <img> → input returned unchanged."""
        html = "<div><p>plain text</p></div>"
        new_html, changed = zot._compress_images_in_html(html)
        assert not changed
        assert new_html == html

    def test_line_wrapped_base64_compressed(self, zot):
        """base64 wrapped at 76 chars with CRLF (org's inline format) → compressed."""
        data = _make_jpeg(3000, 2000)
        b64 = _b64_wrap(data)
        html = f'<div><img src="data:image/jpg;base64,{b64}"></div>'

        new_html, changed = zot._compress_images_in_html(html)

        assert changed
        assert len(new_html) < len(html)
        m = re.search(r'data:image/[^;]+;base64,([A-Za-z0-9+/=]+)', new_html)
        assert m, "expected a compressed base64 data URI (unwrapped, single line)"
        new_img = Image.open(io.BytesIO(base64.b64decode(m.group(1))))
        assert max(new_img.size) <= 1920


# ---------------------------------------------------------------------------
# _prep_upload_file (attachment dispatch)
# ---------------------------------------------------------------------------

class TestPrepUploadFile:
    def test_html_compressed(self, zot, tmp_path):
        """HTML with a large base64 image → dispatched to compression."""
        data = _make_jpeg(3000, 2000)
        b64 = base64.b64encode(data).decode("ascii")
        html = f'<html><body><img src="data:image/jpeg;base64,{b64}"></body></html>'
        src = tmp_path / "page.html"
        src.write_text(html, encoding="utf-8")

        upload_path, cleanup_path = zot._prep_upload_file(str(src))

        assert upload_path != str(src)
        assert cleanup_path is not None
        assert os.path.exists(cleanup_path)
        new_html = open(upload_path, encoding="utf-8").read()
        assert len(new_html) < len(html)

    def test_html_no_images_unchanged(self, zot, tmp_path):
        """HTML without any <img> → passed through unchanged, no temp file."""
        html = "<html><body><p>no images here</p></body></html>"
        src = tmp_path / "noimg.html"
        src.write_text(html, encoding="utf-8")

        upload_path, cleanup_path = zot._prep_upload_file(str(src))

        assert upload_path == str(src)
        assert cleanup_path is None

    def test_standalone_image_compressed(self, zot, tmp_path):
        """Standalone .jpg → compressed to a new temp file."""
        data = _make_jpeg(3000, 2000)
        src = tmp_path / "photo.jpg"
        src.write_bytes(data)

        upload_path, cleanup_path = zot._prep_upload_file(str(src))

        assert upload_path != str(src)
        assert cleanup_path is not None
        assert os.path.exists(cleanup_path)

    def test_other_binary_skipped(self, zot, tmp_path):
        """Non-image binary (PDF) → passed through unchanged, no temp file."""
        src = tmp_path / "doc.pdf"
        src.write_bytes(b"%PDF-1.4 fake pdf content")

        upload_path, cleanup_path = zot._prep_upload_file(str(src))

        assert upload_path == str(src)
        assert cleanup_path is None


# ---------------------------------------------------------------------------
# _note_set (note write path)
# ---------------------------------------------------------------------------

class TestNoteSetCompression:
    def test_note_set_compresses_inlined_images(self, zot, monkeypatch):
        """_note_set compresses base64-inlined images before writing the note."""
        data = _make_jpeg(3000, 2000)
        b64 = base64.b64encode(data).decode("ascii")
        html = f'<div><img src="data:image/jpeg;base64,{b64}"></div>'

        captured = {}

        def fake_create_items(items):
            captured["note"] = items[0]["note"]
            return {"successful": {"0": {"key": "ABCDEFGH"}}, "failed": {}}

        # zot.py's module-level `zot` global is the Zotero client
        client = zot.zot
        monkeypatch.setattr(client, "create_items", fake_create_items)

        zot._note_set("FAKEKEY", html)

        assert "note" in captured
        assert len(captured["note"]) < len(html)


class TestClientTimeout:
    def test_client_write_timeout_relaxed(self, zot):
        """httpx 默认 5s write 超时写不动 MB 级 note；必须放宽到 ≥60s。"""
        assert zot.zot.client.timeout.write >= 60


# ---------------------------------------------------------------------------
# _compress_image_bytes 参数化（附件路径行为不变，note 路径可传更小尺寸/质量）
# ---------------------------------------------------------------------------

class TestCompressImageBytesParam:
    def test_max_dim_override(self, zot):
        """max_dim 参数覆盖默认 1920。"""
        data = _make_jpeg(3000, 2000)
        result = zot._compress_image_bytes(data, max_dim=800)
        img = Image.open(io.BytesIO(result))
        assert max(img.size) <= 800

    def test_quality_override_smaller(self, zot):
        """同尺寸下，quality 更低 → 字节更小。"""
        data = _make_jpeg(3000, 2000)
        hi = zot._compress_image_bytes(data, max_dim=1280, quality=90)
        lo = zot._compress_image_bytes(data, max_dim=1280, quality=40)
        assert len(lo) < len(hi)

    def test_defaults_unchanged(self, zot):
        """不带参数时仍走默认 1920/80，附件路径行为不变。"""
        data = _make_jpeg(3000, 2000)
        result = zot._compress_image_bytes(data)
        img = Image.open(io.BytesIO(result))
        assert max(img.size) <= 1920

    def test_as_jpeg_converts_png(self, zot):
        """as_jpeg=True 时照片类 PNG → JPEG（有损），用于 note 体积预算。"""
        png = _make_noisy_png(500, 400)
        result = zot._compress_image_bytes(png, max_dim=500, quality=70, as_jpeg=True)
        assert result[:3] == b"\xff\xd8\xff"  # JPEG magic bytes


# ---------------------------------------------------------------------------
# _fit_note_under_limit（note 体积预算：≤ _NOTE_MAX_BYTES）
# ---------------------------------------------------------------------------

class TestFitNoteUnderLimit:
    def test_no_images_unchanged(self, zot):
        """无图 note 原样返回。"""
        html = "<div><p>plain text</p></div>"
        assert zot._fit_note_under_limit(html) == html

    def test_single_image_keeps_high_res(self, zot):
        """单张大图 → 阶梯第一档(1280)就够，保持较高清晰度。"""
        data = _make_jpeg(3000, 2000)
        b64 = base64.b64encode(data).decode("ascii")
        html = f'<div><img src="data:image/jpeg;base64,{b64}"></div>'

        new_html = zot._fit_note_under_limit(html)

        assert len(new_html) <= zot._NOTE_MAX_BYTES
        m = re.search(r'data:image/jpeg;base64,([A-Za-z0-9+/=]+)', new_html)
        assert m, "expected a compressed base64 data URI"
        img = Image.open(io.BytesIO(base64.b64decode(m.group(1))))
        assert max(img.size) <= 1280  # 第一档，不降到底

    def test_many_images_fit_without_stripping(self, zot):
        """多张图 → 阶梯降档到 ≤ 上限，但图片仍在（压缩而非删除）。"""
        data = _make_jpeg(3000, 2000)
        b64 = base64.b64encode(data).decode("ascii")
        html = ''.join(f'<img src="data:image/jpeg;base64,{b64}">' for _ in range(3))

        new_html = zot._fit_note_under_limit(html)

        assert len(new_html) <= zot._NOTE_MAX_BYTES
        assert new_html.count("<img") == 3  # 图片未被删除

    def test_extreme_case_strips_images_as_hard_guarantee(self, zot):
        """病态情况(图太多) → 阶梯走完仍超限 → 去图，硬保证 ≤ 上限。"""
        data = _make_jpeg(3000, 2000)
        b64 = base64.b64encode(data).decode("ascii")
        html = ''.join(f'<img src="data:image/jpeg;base64,{b64}">' for _ in range(12))

        new_html = zot._fit_note_under_limit(html)

        assert len(new_html) <= zot._NOTE_MAX_BYTES
        assert "<img" not in new_html  # 兜底：去图

    def test_large_png_converted_to_jpeg_not_stripped(self, zot):
        """note 里的照片类 PNG（无损压不动）→ 转 JPEG 压到 ≤ 上限，而非被删成 [图片]。"""
        png = _make_noisy_png(500, 400)
        b64 = base64.b64encode(png).decode("ascii")
        html = f'<img src="data:image/png;base64,{b64}">'

        new_html = zot._fit_note_under_limit(html)

        assert len(new_html) <= zot._NOTE_MAX_BYTES
        assert new_html.count("<img") == 1  # 图片保留（转 JPEG），未被删
        m = re.search(r'data:image/([a-zA-Z0-9.+-]+);base64,', new_html)
        assert m and m.group(1) == "jpeg"  # 已转 JPEG

    def test_line_wrapped_base64_not_stripped(self, zot):
        """org 导出的内嵌图 base64 按 76 字符换行(CRLF)——必须压缩保留，而非被删成 [图片]。"""
        data = _make_jpeg(3000, 2000)
        b64 = _b64_wrap(data)
        html = f'<img src="data:image/jpg;base64,{b64}">'

        new_html = zot._fit_note_under_limit(html)

        assert len(new_html) <= zot._NOTE_MAX_BYTES
        assert new_html.count("<img") == 1  # 图片保留，未被删成 [图片]

    def test_strips_width_height_attributes(self, zot):
        """org 的 #+attr_html: :width 80% 导出为 width="80%"；Zotero 提取内嵌图为附件时
        会丢 % 变成 width="80"（80px），导致图极小。note 路径应去掉 width/height，
        让 Zotero 按自然尺寸渲染（配合 max-width:100%）。"""
        data = _make_jpeg(3000, 2000)
        b64 = base64.b64encode(data).decode("ascii")
        html = f'<img src="data:image/jpeg;base64,{b64}" alt="x" width="80%" height="44.64">'

        new_html = zot._fit_note_under_limit(html)

        assert "<img" in new_html
        assert "width=" not in new_html
        assert "height=" not in new_html


# ---------------------------------------------------------------------------
# _create_note（fail 捕捉：pyzotero create_items 不检查 failed 字典）
# ---------------------------------------------------------------------------

class TestCreateNote:
    def test_failed_response_returns_false(self, zot, monkeypatch, capsys):
        """create_items 返回非空 failed 字典 → 返回 False 并打印错误，不误报成功。"""
        def fake_create_items(items):
            return {"successful": {}, "failed": {"0": {"code": 413, "message": "Note ... too long"}}}
        monkeypatch.setattr(zot.zot, "create_items", fake_create_items)

        assert zot._create_note("FAKEKEY", "<p>hi</p>") is False
        assert "too long" in capsys.readouterr().out

    def test_successful_response_returns_true(self, zot, monkeypatch):
        def fake_create_items(items):
            return {"successful": {"0": {"key": "ABCDEFGH"}}, "failed": {}}
        monkeypatch.setattr(zot.zot, "create_items", fake_create_items)

        assert zot._create_note("FAKEKEY", "<p>hi</p>") is True

    def test_non_dict_response_treated_as_failure(self, zot, monkeypatch):
        """防御：create_items 返回 None（异常）时不误报成功。"""
        def fake_create_items(items):
            return None
        monkeypatch.setattr(zot.zot, "create_items", fake_create_items)

        assert zot._create_note("FAKEKEY", "<p>hi</p>") is False


# ---------------------------------------------------------------------------
# _note_set 失败时退出（而非打印 ✅）
# ---------------------------------------------------------------------------

class TestNoteSetFailure:
    def test_note_set_exits_on_failed_response(self, zot, monkeypatch, capsys):
        """写失败(failed 非空)时 sys.exit(1) 且不打印 ✅。"""
        def fake_create_items(items):
            return {"successful": {}, "failed": {"0": {"code": 413, "message": "too long"}}}
        monkeypatch.setattr(zot.zot, "create_items", fake_create_items)

        with pytest.raises(SystemExit) as exc_info:
            zot._note_set("FAKEKEY", "<p>hi</p>")

        assert exc_info.value.code == 1
        assert "✅" not in capsys.readouterr().out


# ---------------------------------------------------------------------------
# 删除守卫：delete_item 传单个 dict = 软删（回收站）；传 list = 批量硬删（purge）
# ---------------------------------------------------------------------------

class TestDeleteGuards:
    def test_item_remove_never_batch_hard_deletes(self, zot, monkeypatch):
        """item remove 软删单条：即使 zot.item 返回 list，也只对 items[0]（单个 dict）软删，
        绝不把整个 list 传给 delete_item（那会触发批量硬删/purge、不进回收站）。"""
        fake = {"key": "FAKEKEY", "version": 1, "data": {}}
        captured = {}

        monkeypatch.setattr(zot.zot, "item", lambda key: [fake, fake, fake])
        monkeypatch.setattr(zot.zot, "delete_item", lambda item: captured.__setitem__("arg", item))

        zot._item_remove("FAKEKEY")

        assert isinstance(captured["arg"], dict)
        assert captured["arg"] is fake

    def test_detach_attachment_never_batch_hard_deletes(self, zot, monkeypatch):
        """attachment remove 同样软删单条，不做批量硬删。"""
        fake = {"key": "CHILDKEY", "version": 1,
                "data": {"itemType": "note", "title": "x", "parentItem": "PARENT"}}
        captured = {}

        monkeypatch.setattr(zot.zot, "item", lambda key: fake)
        monkeypatch.setattr(zot.zot, "delete_item", lambda item: captured.__setitem__("arg", item))

        zot.detach_attachment("CHILDKEY")

        assert isinstance(captured["arg"], dict)
        assert captured["arg"] is fake
