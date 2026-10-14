"""Builds the list of supermarkets, pharmacies and cash machines that the open_in_de_buurt tool searches, from
OpenStreetMap.

The public Overpass servers are not meant to back a service with a query per question, so this script asks once for
everything in the Netherlands and keeps it in a file. Run it about once a month:

    uv run python scripts/osm_winkels.py

It also keeps where OpenStreetMap puts the centre of each city, town and village, for a question that names only a
town, and for each place without an address in OpenStreetMap the street of the nearest address, from PDOK. That takes
a call to PDOK per place, one after the other, so the script runs for about ten minutes.

The file is a derived database of OpenStreetMap and so falls under the ODbL, like the data itself: © OpenStreetMap
contributors, https://www.openstreetmap.org/copyright. This script is the method of making it.
"""

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

from nederland_mcp.bronnen.winkels import misplaced
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
# PDOK only knows the Netherlands in Europe; the islands in the Caribbean are in the list too.
EUROPE = (50.7, 53.7, 3.2, 7.3)
# A box around the Netherlands, which is lighter for Overpass than the country's area. The few towns across the border
# do no harm: a town is matched by name, and only near where PDOK has it.
CENTRES = """[out:json][timeout:180][bbox:{0},{2},{1},{3}];
node["place"~"^(city|town|village)$"];
out;""".format(*EUROPE)
KINDS = {
    ("shop", "supermarket"): "supermarkt", ("amenity", "pharmacy"): "apotheek", ("amenity", "atm"): "pinautomaat",
}
# The names a town goes by, so Den Haag is found under 's-Gravenhage too.
NAMES = ("name", "official_name", "alt_name", "short_name", "name:nl")
REVERSE = "https://api.pdok.nl/bzk/locatieserver/search/v3_1/reverse"
# The nearest address counts if it is this close. Further away it can be across a square or in another street.
STREET_M = 60
OUT = Path(__file__).parent.parent / "src" / "nederland_mcp" / "data" / "osm_winkels.json"
FIELDS = ["lat", "lon", "naam", "merk", "adres", "openingstijden", "straat"]


def address(tags: dict) -> str:
    street = " ".join(part for part in (tags.get("addr:street"), tags.get("addr:housenumber")) if part)
    return ", ".join(part for part in (street, tags.get("addr:city")) if part)


def overpass(query: str) -> dict:
    response = httpx.post(OVERPASS, data={"data": query}, headers={"User-Agent": USER_AGENT}, timeout=700)
    response.raise_for_status()
    return response.json()


def places(data: dict) -> dict[str, list]:
    found: dict[str, list] = {kind: [] for kind in KINDS.values()}
    for element in data.get("elements", []):
        tags = element.get("tags") or {}
        kind = next((name for (key, value), name in KINDS.items() if tags.get(key) == value), None)
        point = element.get("center") or element
        if not kind or "lat" not in point:
            continue
        # Closed shops sometimes keep their tags with a prefix; those have no shop= or amenity= left, so they are out.
        name = tags.get("name") or tags.get("brand") or tags.get("operator") or ""
        # Bitcoin machines filed as cash machines, and liquor stores and other shops filed as supermarkets.
        if misplaced(kind, f"{name} {tags.get('brand') or ''}"):
            continue
        found[kind].append([
            round(point["lat"], 5), round(point["lon"], 5), name, tags.get("brand") or tags.get("operator") or "",
            address(tags), tags.get("opening_hours") or "", "",
        ])
    for rows in found.values():
        rows.sort()
    return found


def centres(data: dict) -> list[list]:
    """Each city, town and village with its point and names. Its place node marks the centre."""
    rows = []
    for element in data.get("elements", []):
        tags = element.get("tags") or {}
        names = [name.strip() for key in NAMES for name in (tags.get(key) or "").split(";") if name.strip()]
        if names and "lat" in element:
            rows.append([round(element["lat"], 5), round(element["lon"], 5), *dict.fromkeys(names)])
    return sorted(rows)


def streets(found: dict[str, list]) -> int:
    """Fills in the street of the nearest address for each place without an address, and says how many."""
    south, north, west, east = EUROPE
    todo = [row for rows in found.values() for row in rows if not row[4] and south < row[0] < north and west < row[1] < east]
    filled = 0
    with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=30) as client:
        for row in todo:
            params = {
                "lat": row[0], "lon": row[1], "type": "adres", "rows": 1, "distance": STREET_M,
                "fl": "straatnaam,woonplaatsnaam",
            }
            try:
                response = client.get(REVERSE, params=params)
                response.raise_for_status()
                docs = response.json().get("response", {}).get("docs", [])
            except (httpx.HTTPError, ValueError):
                docs = []
            if docs and docs[0].get("straatnaam"):
                row[6] = ", ".join(part for part in (docs[0]["straatnaam"], docs[0].get("woonplaatsnaam")) if part)
                filled += 1
            # One at a time and not too fast: PDOK is free for everyone.
            time.sleep(0.1)
    return filled


def write(found: dict[str, list], towns: list[list], made: str) -> None:
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(
        json.dumps(
            {
                "licentie": "© OpenStreetMap-bijdragers, ODbL 1.0, https://www.openstreetmap.org/copyright",
                "stand": made,
                "velden": FIELDS,
                "plekken": found,
                "centra": towns,
            },
            ensure_ascii=False, separators=(",", ":"),
        ) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    started = time.monotonic()
    data = overpass(QUERY)
    found = places(data)
    towns = centres(overpass(CENTRES))
    filled = streets(found)
    made = data.get("osm3s", {}).get("timestamp_osm_base") or datetime.now(timezone.utc).isoformat(timespec="seconds")
    write(found, towns, made)
    counts = ", ".join(f"{len(rows)} {kind}" for kind, rows in found.items())
    print(
        f"{counts}, {len(towns)} centra, {filled} straten van PDOK (stand {made}) in {time.monotonic() - started:.0f} s "
        f"naar {OUT}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
