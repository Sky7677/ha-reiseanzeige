"""Erzeugt Reisen mit dem Python-Teil (Text + nachgebautes OpenBoards) als
Testdaten für die Karte. So prüfen die Kartentests das Format, das die
Integration wirklich liefert:
  * frontend/test/fixtures/moritzplatz-berlin.python.json – ohne Streckenverlauf,
  * frontend/test/fixtures/umleitung.python.json – ICE ab Erfurt über Leipzig
    statt Halle, mit Verlauf aus dem Test-Gleisnetz."""

import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "custom_components" / "reiseanzeige"), str(ROOT / "tests")]

from core.geometry import apply_geometry  # noqa: E402
from core.boardmatch import match_snapshot  # noqa: E402
from core.geometry import fill_coordinates  # noqa: E402
from core.journey import draft_snapshot  # noqa: E402
from core.places import Places  # noqa: E402
from core.railnet.router import RailNet  # noqa: E402
from core.realtime import leg_realtime  # noqa: E402
from core.share_text import parse_share_text  # noqa: E402
from fake_openboards import leipzig_instead_of_halle, november_world  # noqa: E402
from railnet_fixture import world  # noqa: E402

TZ = ZoneInfo("Europe/Berlin")
COORDS = {  # ungefähre echte Lage
    "8000013": (48.3654, 10.8856), "8000080": (48.7143, 10.7717), "8000122": (48.9567, 10.9087),
    "8000284": (49.4457, 11.0827), "8010101": (50.9725, 11.0380), "8010159": (51.4776, 11.9870),
    "8011113": (52.4753, 13.3655), "8089110": (52.4753, 13.3655), "8089032": (52.4801, 13.3510),
    "8089017": (52.4782, 13.3430), "8089015": (52.4800, 13.3125), "8010205": (51.3455, 12.3821),
}


async def main():
    w = november_world()
    text = (ROOT / "tests" / "fixtures" / "share" / "moritzplatz-berlin-2026-11-01.txt").read_text(encoding="utf-8")
    snap = draft_snapshot(parse_share_text(text), datetime(2026, 11, 1, 14, 17, tzinfo=TZ))
    snap = await match_snapshot(snap, w, None, w.directory())
    snap["createdAt"] = "2026-11-01T14:17:00+01:00"
    fill_coordinates(snap, None, lambda eva, name: COORDS.get(eva))
    w.messages = {"disruptions": [{"messageID": 1, "descriptions": [{"language": "deu", "text": "Bauarbeiten zwischen Halle und Berlin"}]}],
                  "risCauseCodes": [{"messageID": 2, "code": "38", "descriptions": [{"language": "deu", "text": "Reparatur am Zug"}]}]}
    w.change("-222", 1, delay=5, platform="2", messages=[1, 2])
    w.change("-222", 2, delay=5)   # OpenBoards nennt die Prognose an allen kommenden Halten
    w.change("-222", 3, delay=5)
    rt = await leg_realtime(snap["legs"][2], [1, 2, 3], None, w, datetime(2026, 11, 1, 17, 30, tzinfo=TZ))
    out = ROOT / "frontend" / "test" / "fixtures" / "moritzplatz-berlin.python.json"
    out.write_text(json.dumps({"snapshot": snap, "realtime": {"legs": {"2": rt}}}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(out.relative_to(ROOT))

    # Umleitung: neuer Laufweg ab Erfurt, Verlauf über das Test-Gleisnetz
    leipzig_instead_of_halle(w)
    net = RailNet(world())
    rt = await leg_realtime(snap["legs"][2], [1, 2, 3], None, w, datetime(2026, 11, 1, 17, 30, tzinfo=TZ))
    apply_geometry(snap, net, Places(net.stations, net.routes), version="test", diversions={2: rt["diversion"]})
    out = ROOT / "frontend" / "test" / "fixtures" / "umleitung.python.json"
    out.write_text(json.dumps({"snapshot": snap, "realtime": {"legs": {"2": rt}}}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(out.relative_to(ROOT))

asyncio.run(main())
