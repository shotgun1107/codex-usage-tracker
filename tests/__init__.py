"""Test package bootstrap for the src layout."""

from pathlib import Path
import sys


SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC_ROOT))
