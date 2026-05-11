"""Render the first page of a paper PDF to a JPEG cover image."""
from __future__ import annotations

import io
import logging
import shutil
import tempfile
from pathlib import Path

import fitz  # pymupdf
import requests
from PIL import Image

log = logging.getLogger(__name__)

USER_AGENT = "redpaper/0.1 (+https://github.com/Nangongyeee/redpaper)"
TIMEOUT_SECONDS = 60


def download_pdf(url: str, dest: Path) -> bool:
    """Download a PDF to `dest`. Returns True on success."""
    if not url:
        return False
    try:
        with requests.get(
            url,
            stream=True,
            timeout=TIMEOUT_SECONDS,
            headers={"User-Agent": USER_AGENT},
        ) as r:
            r.raise_for_status()
            with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
                tmp_path = Path(tmp.name)
                shutil.copyfileobj(r.raw, tmp)
        tmp_path.rename(dest)
        return True
    except Exception as e:
        log.warning("download_pdf failed for %s: %s", url, e)
        return False


def render_first_page(pdf_path: Path, out_jpg: Path, max_width: int = 900, quality: int = 82) -> tuple[bool, int]:
    """Render the first page of `pdf_path` to a JPEG.

    Returns (success, page_count).
    """
    try:
        doc = fitz.open(pdf_path)
        page_count = doc.page_count
        page = doc.load_page(0)
        # 2x zoom for crisper text, then resize down
        pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
        img = Image.open(io.BytesIO(pix.tobytes("png")))
        if img.width > max_width:
            ratio = max_width / img.width
            new_size = (max_width, int(img.height * ratio))
            img = img.resize(new_size, Image.LANCZOS)
        img = img.convert("RGB")
        out_jpg.parent.mkdir(parents=True, exist_ok=True)
        img.save(out_jpg, "JPEG", quality=quality, optimize=True)
        doc.close()
        return True, page_count
    except Exception as e:
        log.warning("render_first_page failed for %s: %s", pdf_path, e)
        return False, 0


def fetch_and_render(pdf_url: str, paper_id: str, covers_dir: Path) -> tuple[str | None, int]:
    """Download a PDF, render its first page.
    Returns (site_relative_path_or_None, page_count_or_0).
    """
    out_jpg = covers_dir / f"{paper_id}.jpg"
    if out_jpg.exists():
        # Cached image — page_count not known unless we re-open the pdf, return 0.
        return _to_site_rel(out_jpg), 0

    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
        tmp_path = Path(tmp.name)
    try:
        if not download_pdf(pdf_url, tmp_path):
            return None, 0
        ok, pages = render_first_page(tmp_path, out_jpg)
        if not ok:
            return None, 0
        return _to_site_rel(out_jpg), pages
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass


def _to_site_rel(p: Path) -> str:
    """Return path relative to the `site/` directory, with forward slashes."""
    from .config import SITE_DIR
    try:
        rel = p.resolve().relative_to(SITE_DIR.resolve())
    except ValueError:
        return str(p)
    return rel.as_posix()
