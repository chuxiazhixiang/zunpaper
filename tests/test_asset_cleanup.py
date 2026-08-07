from __future__ import annotations

import sys
import unittest
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))

from redpaper.asset_cleanup import prune_cover_assets


@dataclass
class DummyPaper:
    id: str
    cover_image: str = ""
    preview_pages: list[str] = field(default_factory=list)


def _image(path: Path, width: int = 1200) -> None:
    Image.new("RGB", (width, 900), (220, 220, 220)).save(path, "JPEG", quality=90)


class AssetCleanupTests(unittest.TestCase):
    def test_prunes_extra_previews_orphans_and_optimizes(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as raw:
            tmp_path = Path(raw)
            self._test_prunes_extra_previews_orphans_and_optimizes(tmp_path)

    def _test_prunes_extra_previews_orphans_and_optimizes(self, tmp_path: Path) -> None:
        site = tmp_path / "site"
        covers = site / "assets" / "img" / "covers"
        covers.mkdir(parents=True)
        _image(covers / "paper-a.jpg")
        _image(covers / "paper-a-p2.jpg")
        _image(covers / "paper-a-p3.jpg")
        _image(covers / "orphan.jpg")

        paper = DummyPaper(
            "paper-a",
            "assets/img/covers/paper-a.jpg",
            [
                "assets/img/covers/paper-a-p2.jpg",
                "assets/img/covers/paper-a-p3.jpg",
            ],
        )
        stats = prune_cover_assets([paper], covers, max_preview_pages=1, max_width=800)

        self.assertEqual(paper.preview_pages, ["assets/img/covers/paper-a-p2.jpg"])
        self.assertFalse((covers / "paper-a-p3.jpg").exists())
        self.assertFalse((covers / "orphan.jpg").exists())
        with Image.open(covers / "paper-a.jpg") as optimized:
            self.assertEqual(optimized.width, 800)
        self.assertEqual(stats.removed_files, 2)
        self.assertEqual(stats.trimmed_ids, ["paper-a"])
        self.assertEqual(stats.optimized_files, 2)


    def test_remote_cover_does_not_keep_unrelated_local_file(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as raw:
            site = Path(raw) / "site"
            covers = site / "assets" / "img" / "covers"
            covers.mkdir(parents=True)
            _image(covers / "orphan.jpg", width=400)
            paper = DummyPaper("remote", "https://example.com/cover.jpg")

            stats = prune_cover_assets([paper], covers)

            self.assertEqual(stats.removed_files, 1)
            self.assertFalse((covers / "orphan.jpg").exists())


if __name__ == "__main__":
    unittest.main()
