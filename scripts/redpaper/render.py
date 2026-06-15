"""Render the first few pages of a paper PDF to JPEGs.

* Page 1 = the cover image (title + abstract + sometimes Figure 1)
* Pages 2-4 are stored as additional preview images and surfaced as carousel
  slides on the detail page. Architecture / pipeline figures usually appear
  on page 2 or 3 of recent ML/robotics papers, so showing them inline lets
  the reader judge a paper without leaving the site. ("流程图")
"""
from __future__ import annotations

import io
import logging
import re
import shutil
import tempfile
from pathlib import Path

import fitz  # pymupdf
import requests
from PIL import Image

log = logging.getLogger(__name__)

USER_AGENT = "redpaper/0.1 (+https://github.com/Nangongyeee/redpaper)"
TIMEOUT_SECONDS = 60
PREVIEW_PAGES = 4  # how many pages to render in total (page 1 + 3 more)


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


def extract_head_text(pdf_url: str, max_pages: int = 2, max_chars: int = 3500) -> tuple[str, int]:
    """Download a PDF and return (首页文本, 总页数)。

    给 enrich 用：论文的**真实机构 / 单位脚注**几乎只在首页（不在摘要里），
    具体机器人平台型号、仿真器也常出现在首页 / 引言。把这段文本喂给抽取器，
    就不用让它从作者名 / 主题瞎猜了。顺带返回总页数，让 page_count（longer_paper
    评分用）在 enrich 迁移时一并回填，省得为数页数单独再下一次 PDF。
    Best-effort：任何失败返回 ('', 0)。
    """
    if not pdf_url:
        return "", 0
    with tempfile.TemporaryDirectory() as td:
        dest = Path(td) / "head.pdf"
        if not download_pdf(pdf_url, dest):
            return "", 0
        try:
            doc = fitz.open(dest)
        except Exception as e:
            log.warning("extract_head_text open failed: %s", e)
            return "", 0
        parts: list[str] = []
        page_count = 0
        try:
            page_count = int(doc.page_count or 0)
            for i in range(min(max_pages, doc.page_count)):
                try:
                    parts.append(doc[i].get_text() or "")
                except Exception:
                    pass
        finally:
            doc.close()
    text = "\n".join(parts)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text[:max_chars], page_count


def _render_page(doc, page_idx: int, out_jpg: Path, max_width: int = 900, quality: int = 82) -> bool:
    try:
        page = doc.load_page(page_idx)
        pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
        img = Image.open(io.BytesIO(pix.tobytes("png")))
        if img.width > max_width:
            ratio = max_width / img.width
            img = img.resize((max_width, int(img.height * ratio)), Image.LANCZOS)
        img = img.convert("RGB")
        out_jpg.parent.mkdir(parents=True, exist_ok=True)
        img.save(out_jpg, "JPEG", quality=quality, optimize=True)
        return True
    except Exception as e:
        log.warning("render page %d failed: %s", page_idx, e)
        return False


def render_pages(pdf_path: Path, paper_id: str, covers_dir: Path) -> tuple[str | None, list[str], int]:
    """Render up to PREVIEW_PAGES pages of `pdf_path` to JPEGs.

    Returns:
        cover_path: site-relative path to page 1 jpg (or None on failure)
        preview_paths: site-relative paths to additional pages (pages 2..N),
                       in document order. Empty list if the paper has <2 pages.
        page_count: total pages in the PDF (0 on failure).

    File layout:
        site/assets/img/covers/{paper_id}.jpg        ← cover (page 1)
        site/assets/img/covers/{paper_id}-p2.jpg     ← page 2
        site/assets/img/covers/{paper_id}-p3.jpg     ← page 3
        ...
    """
    try:
        doc = fitz.open(pdf_path)
    except Exception as e:
        log.warning("open pdf failed: %s", e)
        return None, [], 0

    page_count = doc.page_count
    cover_jpg = covers_dir / f"{paper_id}.jpg"
    cover_rel: str | None = None
    if _render_page(doc, 0, cover_jpg):
        cover_rel = _to_site_rel(cover_jpg)

    preview_rels: list[str] = []
    for idx in range(1, min(PREVIEW_PAGES, page_count)):
        page_jpg = covers_dir / f"{paper_id}-p{idx + 1}.jpg"
        if page_jpg.exists() or _render_page(doc, idx, page_jpg):
            preview_rels.append(_to_site_rel(page_jpg))

    doc.close()
    return cover_rel, preview_rels, page_count


def fetch_and_render(pdf_url: str, paper_id: str, covers_dir: Path) -> tuple[str | None, list[str], int]:
    """Download a PDF, render its first few pages.

    Returns (cover_path_or_None, preview_paths_list, page_count_or_0).

    Caching:
    - If the cover AND all PREVIEW_PAGES-1 page jpgs exist on disk, skip the
      PDF download entirely (full hit).
    - If only the cover exists but some/all preview pages are missing, we
      STILL download the PDF and render the missing pages. This is the case
      that bit us before: papers cached in the cover-only era were never
      getting multi-page previews even after the feature shipped, because
      we early-returned as soon as the cover jpg existed.
    """
    cover_jpg = covers_dir / f"{paper_id}.jpg"
    expected_preview_count = max(0, PREVIEW_PAGES - 1)

    if cover_jpg.exists():
        existing_previews: list[str] = []
        missing_any = False
        for idx in range(2, PREVIEW_PAGES + 1):
            p = covers_dir / f"{paper_id}-p{idx}.jpg"
            if p.exists():
                existing_previews.append(_to_site_rel(p))
            else:
                missing_any = True

        if not missing_any or expected_preview_count == 0:
            # Full hit — no need to re-download the PDF.
            return _to_site_rel(cover_jpg), existing_previews, 0

        # Partial hit: cover present but at least one preview page is missing.
        # Re-fetch the PDF and render the missing pages. render_pages itself
        # is idempotent (it overwrites existing files / re-renders missing ones).
        log.info("partial cache for %s: cover ok, %d/%d previews missing — re-fetching",
                 paper_id, expected_preview_count - len(existing_previews), expected_preview_count)

    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
        tmp_path = Path(tmp.name)
    try:
        if not download_pdf(pdf_url, tmp_path):
            # Couldn't fetch — fall back to whatever we have on disk.
            if cover_jpg.exists():
                existing_previews = []
                for idx in range(2, PREVIEW_PAGES + 1):
                    p = covers_dir / f"{paper_id}-p{idx}.jpg"
                    if p.exists():
                        existing_previews.append(_to_site_rel(p))
                return _to_site_rel(cover_jpg), existing_previews, 0
            return None, [], 0
        cover_rel, preview_rels, pages = render_pages(tmp_path, paper_id, covers_dir)
        return cover_rel, preview_rels, pages
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
