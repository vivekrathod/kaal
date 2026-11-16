#!/usr/bin/env python3
"""Launch the Kaal CLI from a source checkout."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kaal.cli import main  # noqa: E402

if __name__ == "__main__":
    main()
