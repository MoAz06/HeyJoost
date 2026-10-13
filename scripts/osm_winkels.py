"""Builds the list of supermarkets, pharmacies and cash machines that the open_in_de_buurt tool searches, from
OpenStreetMap.

The public Overpass servers are not meant to back a service with a query per question, so this script asks once for
everything in the Netherlands and keeps it in a file. Run it about once a month:

    uv run python scripts/osm_winkels.py

The file is a derived database of OpenStreetMap and so falls under the ODbL, like the data itself: © OpenStreetMap
contributors, https://www.openstreetmap.org/copyright. This script is the method of making it.
"""

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

from nederland_mcp.common import USER_AGENT

OVERPASS = "https://overpass-api.de/api/interpreter"
QUERY = """[out:json][timeout:600];
area["ISO3166-1"="NL"][admin_level=2]->.nl;
(
  nwr["shop"="supermarket"](area.nl);
  nwr["amenity"="pharmacy"](area.nl);
  nwr["amenity"="atm"](area.nl);
);
out center tags;"""
KINDS = {
    ("shop", "supermarket"): "supermarkt", ("amenity", "pharmacy"): "apotheek", ("amenity", "atm"): "pinautomaat",
}
# Machines for bitcoin are tagged as cash machines too, but give no cash.
CRYPTO = ("bitcoin", "crypto", "coin", "btc")
OUT = Path(__file__).parent.parent / "src" / "nederland_mcp" / "data" / "osm_winkels.json"


def address(tags: dict) -> str:
    street = " ".join(part for part in (tags.get("addr:street"), tags.get("addr:housenumber")) if part)
    return ", ".join(part for part in (street, tags.get("addr:city")) if part)


def main() -> int:
    started = time.monotonic()
    response = httpx.post(OVERPASS, data={"data": QUERY}, headers={"User-Agent": USER_AGENT}, timeout=700)
    response.raise_for_status()
    data = response.json()
    places: dict[str, list] = {kind: [] for kind in KINDS.values()}
    for element in data.get("elements", []):
        tags = element.get("tags") or {}
        kind = next((name for (key, value), name in KINDS.items() if tags.get(key) == value), None)
        point = element.get("center") or element
        if not kind or "lat" not in point:
            continue
        # Closed shops sometimes keep their tags with a prefix; those have no shop= or amenity= left, so they are out.
        name = tags.get("name") or tags.get("brand") or tags.get("operator") or ""
        if kind == "pinautomaat" and any(word in name.lower() for word in CRYPTO):
            continue
        places[kind].append([
            round(point["lat"], 5), round(point["lon"], 5), name, tags.get("brand") or tags.get("operator") or "",
            address(tags), tags.get("opening_hours") or "",
        ])
    for rows in places.values():
        rows.sort()
    made = data.get("osm3s", {}).get("timestamp_osm_base") or datetime.now(timezone.utc).isoformat(timespec="seconds")
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(
        json.dumps(
            {
                "licentie": "© OpenStreetMap-bijdragers, ODbL 1.0, https://www.openstreetmap.org/copyright",
                "stand": made,
                "velden": ["lat", "lon", "naam", "merk", "adres", "openingstijden"],
                "plekken": places,
            },
            ensure_ascii=False, separators=(",", ":"),
        ) + "\n",
        encoding="utf-8",
    )
    counts = ", ".join(f"{len(rows)} {kind}" for kind, rows in places.items())
    print(f"{counts} (stand {made}) in {time.monotonic() - started:.0f} s naar {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
