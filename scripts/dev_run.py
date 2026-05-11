"""Local dev helper: run the full pipeline against a small slice for quick iteration.

Usage:
    DEV_LIMIT=4 DEV_LOOKBACK=7 python scripts/dev_run.py

Mirrors the production `run()` (digest, rss, enrichment) but limits how many
arXiv results we fetch per channel.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from redpaper import build as build_mod  # noqa: E402
from redpaper import config as cfg  # noqa: E402


def _patch_load_sources_for_dev():
    original = cfg.load_sources
    limit = int(os.environ.get("DEV_LIMIT", "8"))
    lookback = int(os.environ.get("DEV_LOOKBACK", "5"))

    def patched():
        s = original()
        s.arxiv_per_channel_limit = limit
        s.arxiv_lookback_days = lookback
        return s

    cfg.load_sources = patched


def main() -> None:
    _patch_load_sources_for_dev()
    build_mod.run()


if __name__ == "__main__":
    main()
