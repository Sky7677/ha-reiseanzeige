"""Gemeinsame Test-Hilfen.

Die reine Logik (custom_components/reiseanzeige/core) wird als eigenes Paket
"core" importiert, damit die Tests ohne Home Assistant laufen.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "custom_components" / "reiseanzeige"))
