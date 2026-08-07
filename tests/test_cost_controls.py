from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))

from redpaper.cost_control import (
    daily_slot_available,
    mark_daily_slot,
    monthly_digest_is_current,
)


class DummyPaper:
    def __init__(self, paper_id: str):
        self.id = paper_id


class CostControlTests(unittest.TestCase):
    def test_daily_slot_runs_only_once_per_day(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw) / "state.json"
            self.assertTrue(daily_slot_available(state, "discover"))
            mark_daily_slot(state, "discover")
            self.assertFalse(daily_slot_available(state, "discover"))
            self.assertTrue(daily_slot_available(state, "backfill"))

    def test_monthly_digest_skips_unchanged_paper_set(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            digest = Path(raw) / "2026-08.json"
            digest.write_text(json.dumps({"paper_ids": ["a", "b"]}), encoding="utf-8")
            papers = [DummyPaper("b"), DummyPaper("a")]

            self.assertTrue(monthly_digest_is_current(digest, papers))
            papers.append(DummyPaper("c"))
            self.assertFalse(monthly_digest_is_current(digest, papers))

    def test_invalid_digest_cache_is_not_current(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            digest = Path(raw) / "2026-08.json"
            digest.write_text("not json", encoding="utf-8")
            papers = [DummyPaper("a")]

            self.assertFalse(monthly_digest_is_current(digest, papers))


if __name__ == "__main__":
    unittest.main()
