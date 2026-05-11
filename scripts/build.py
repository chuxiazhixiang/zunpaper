"""Entry-point shim so users can run `python scripts/build.py`."""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Ensure the package is importable when this file is run directly.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from redpaper.build import run  # noqa: E402

if __name__ == "__main__":
    run()
