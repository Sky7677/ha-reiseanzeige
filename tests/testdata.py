"""Zugriff auf Testdaten (tests/fixtures)."""

from pathlib import Path

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def fixture_text(*parts: str) -> str:
    return FIXTURES.joinpath(*parts).read_text(encoding="utf-8")
