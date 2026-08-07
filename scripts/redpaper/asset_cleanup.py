"""Keep generated cover assets bounded and consistent with paper JSON files."""
from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from PIL import Image


@dataclass
class CleanupStats:
    """Summary of one cover cleanup pass."""

    removed_files: int = 0
    removed_bytes: int = 0
    optimized_files: int = 0
    optimized_bytes: int = 0
    trimmed_papers: int = 0
    trimmed_ids: list[str] = field(default_factory=list)


def _local_cover_name(ref: str, site_dir: Path, covers_dir: Path) -> str | None:
    """Return a safe local filename for a site-relative cover reference."""
    normalized = str(ref or "").replace("\\", "/")
    prefix = "assets/img/covers/"
    if not normalized.startswith(prefix):
        return None
    try:
        candidate = (site_dir / normalized).resolve()
        candidate.relative_to(covers_dir.resolve())
    except ValueError:
        return None
    return candidate.name


def _optimize_jpeg(path: Path, *, max_width: int, quality: int) -> int:
    """Resize an oversized JPEG in place and return bytes saved."""
    if path.suffix.lower() not in {".jpg", ".jpeg"}:
        return 0
    before = path.stat().st_size
    try:
        with Image.open(path) as image:
            if image.width <= max_width:
                return 0
            ratio = max_width / image.width
            resized = image.resize(
                (max_width, max(1, int(image.height * ratio))),
                Image.Resampling.LANCZOS,
            ).convert("RGB")
            with tempfile.NamedTemporaryFile(
                dir=path.parent, prefix=f".{path.stem}.", suffix=".tmp", delete=False
            ) as tmp:
                tmp_path = Path(tmp.name)
            try:
                resized.save(
                    tmp_path,
                    "JPEG",
                    quality=quality,
                    optimize=True,
                    progressive=True,
                )
                tmp_path.replace(path)
            finally:
                tmp_path.unlink(missing_ok=True)
    except Exception:
        return 0
    return max(0, before - path.stat().st_size)


def prune_cover_assets(
    papers: Iterable[object],
    covers_dir: Path,
    *,
    max_preview_pages: int = 1,
    max_width: int = 800,
    quality: int = 76,
) -> CleanupStats:
    """Trim preview references, delete unreferenced files, and optimize JPGs.

    ``papers`` are mutated in place when they have more than
    ``max_preview_pages`` preview references. The caller should persist papers
    listed in ``CleanupStats.trimmed_ids`` before rebuilding feed indexes.
    References outside ``assets/img/covers/`` are intentionally ignored so a
    remote/manual cover URL cannot cause unrelated local files to be kept.
    """
    covers_dir = covers_dir.resolve()
    site_dir = covers_dir.parents[2]
    max_preview_pages = max(0, int(max_preview_pages))
    stats = CleanupStats()
    keep: set[str] = set()

    for paper in papers:
        previews = list(getattr(paper, "preview_pages", None) or [])
        trimmed = previews[:max_preview_pages]
        if trimmed != previews:
            paper.preview_pages = trimmed
            stats.trimmed_papers += 1
            stats.trimmed_ids.append(str(getattr(paper, "id", "")))
        for ref in [getattr(paper, "cover_image", ""), *trimmed]:
            name = _local_cover_name(ref, site_dir, covers_dir)
            if name:
                keep.add(name)

    covers_dir.mkdir(parents=True, exist_ok=True)
    for asset in covers_dir.iterdir():
        if not asset.is_file():
            continue
        if asset.name not in keep:
            try:
                stats.removed_bytes += asset.stat().st_size
                asset.unlink()
                stats.removed_files += 1
            except OSError:
                continue

    for name in sorted(keep):
        asset = covers_dir / name
        if not asset.is_file():
            continue
        saved = _optimize_jpeg(asset, max_width=max_width, quality=quality)
        if saved:
            stats.optimized_files += 1
            stats.optimized_bytes += saved

    return stats


__all__ = ["CleanupStats", "prune_cover_assets"]
