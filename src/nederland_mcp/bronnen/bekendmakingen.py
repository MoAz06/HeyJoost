"""Official notices near an address, from officielebekendmakingen.nl through the SRU search of KOOP (CC0): permits
asked for or granted (building, felling trees, events), notifications and traffic decisions.

The notices are public, but they often name an address and sometimes a person. So Hey Joost only searches around an
address, never by name or text, and KOOP only gets a point rounded to about a hundred metres and a date. Answers
leave out house numbers and postcodes, and notices about a person (a name, the BRP, a parking place for a licence
plate) are left out altogether. Nothing is kept beyond the short cache.
"""

import re
from datetime import timedelta
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from ..common import cached, get_json, now_local
from .ov import _address, _distance
from .plaats import common_name

SRU = "https://repository.overheid.nl/sru"
BRON = "Officiële bekendmakingen van de overheid, via KOOP (CC0)"
TTL = 30 * 60
MIN_RADIUS, MAX_RADIUS = 50, 500
MAX_WEEKS = 26
MAX_RECORDS = 100
MAX_ITEMS = 15
UITLEG = (
    "Een aanvraag is nog geen besluit. Tegen een besluit, zoals een verleende vergunning, kun je meestal binnen 6 "
    "weken bezwaar maken of in beroep gaan. Hoe en tot wanneer staat in de bekendmaking zelf. Bij een ontwerpbesluit "
    "kun je eerst een zienswijze geven."
)
# Notices about a person rather than a place stay out, also when the title names no one.
PERSONAL = re.compile(
    r"\b(de heer|mevrouw|dhr|mevr|brp|basisregistratie\w*|uitschrijving|inschrijving|naamswijziging|naturalisatie|"
    r"curatele|bewind\w*|overlijden|overleden|geboorte|adresonderzoek|onbekend waarheen|briefadres|"
    r"gehandicapt\w*|kenteken\w*|persoonsgegevens)\b",
    re.IGNORECASE,
)
# An initial and a surname, like J. Jansen or A.B. de Vries.
NAME = re.compile(r"\b(?:[A-Z]\.\s?){1,4}(?:(?:van|de|der|den|het|ter|ten)\s+){0,3}[A-Z][a-z]+")
POSTCODE = re.compile(r"\s*\b\d{4}\s?[A-Z]{2}\b")
NUMBER_WORD = re.compile(r"\s*(?:\b(?:t\.?\s?h\.?\s?v\.?|ter hoogte van|naast|nabij)\s*)?\b(?:nr|nummer|huisnummer)\.?\s*\d+\w*", re.I)
# House numbers after a street, alone, as a list or range or with a letter ("115 B"), but not a count like "3 bomen"
# or a date. Only "en" and "t/m" may follow in lower case, as in "Rapenburg 70 en de Kloksteeg".
NUMBER = r"\d+[A-Za-z]{0,2}(?:\s[A-Z](?![A-Za-z]))?"
HOUSE_NUMBERS = re.compile(
    rf"(?<=[A-Za-zÀ-ÿ)])\s+{NUMBER}(?:\s?(?:-|–|t/m|,|en)\s?{NUMBER})*\b(?![-/.:]\d)(?!\s+(?!en\b|t/m\b)[a-z])"
)
STAGES = (
    ("rectificatie", "rectificatie"), ("ontwerp", "ontwerpbesluit"), ("aanvraag", "aanvraag"),
    ("aangevraagd", "aanvraag"), ("ingediend", "aanvraag"), ("verlenging beslistermijn", "beslistermijn verlengd"),
    ("buiten behandeling", "buiten behandeling"), ("ingetrokken", "ingetrokken"), ("geweigerd", "geweigerd"),
    ("verleend", "verleend"), ("melding", "melding"), ("besluit", "besluit"),
)


def _value(field: Any) -> list[str]:
    """The values of an SRU field, which is a string, a dict with "$", or a list of those."""
    items = field if isinstance(field, list) else [field]
    values = [item.get("$") if isinstance(item, dict) else item for item in items]
    return [value for value in values if isinstance(value, str) and value]


def _points(meta: dict) -> list[dict]:
    """The points a notice is marked on: its points, or else the corners of its area."""
    points, corners = [], []
    for mark in meta.get("tpmeta", {}).get("gebiedsmarkering") or []:
        if not isinstance(mark, dict):
            continue
        point = (mark.get("Punt") or {}).get("locatiepunt")
        if point:
            lat, lon = point.split()[:2]
            points.append({"lat": float(lat), "lon": float(lon)})
        area = (mark.get("Vlak") or {}).get("locatiegebied") or ""
        for lon, lat in re.findall(r"(-?\d+\.\d+) (-?\d+\.\d+)", area):
            corners.append({"lat": float(lat), "lon": float(lon)})
    return points or corners


def _clean(title: str) -> str:
    """The title without house numbers or postcodes: 'Papengracht 40 2311TX Leiden' becomes 'Papengracht, Leiden'."""
    text = POSTCODE.sub(",", title)
    text = NUMBER_WORD.sub("", text)
    text = HOUSE_NUMBERS.sub("", text)
    text = re.sub(r"\s+,", ",", text)
    text = re.sub(r",(\s*,)+", ",", text)
    parts = [part.strip() for part in text.split(",")]
    # The same street can now be there twice, as in 'Apothekersdijk, Leiden, Apothekersdijk, Leiden'.
    kept = [part for number, part in enumerate(parts) if part and part not in parts[:number]]
    return " ".join(", ".join(kept).split())


def _stage(title: str) -> str | None:
    lower = title.lower()
    return next((name for word, name in STAGES if word in lower), None)


def _notice(record: dict, origin: dict) -> dict[str, Any] | None:
    data = record.get("recordData", {}).get("gzd", {})
    meta = data.get("originalData", {}).get("meta", {})
    core, mantle, extra = meta.get("owmskern", {}), meta.get("owmsmantel", {}), meta.get("tpmeta", {})
    title = " ".join((core.get("title") or "").split())
    if not title or PERSONAL.search(title) or NAME.search(title):
        return None
    points = _points(meta)
    if not points:
        return None
    url = (data.get("enrichedData") or {}).get("preferredUrl") or (mantle.get("hasVersion") or {}).get("resourceIdentifier")
    if not url and core.get("identifier"):
        url = f"https://zoek.officielebekendmakingen.nl/{core['identifier']}.html"
    item: dict[str, Any] = {
        "soort": ", ".join(_value(core.get("type"))) or None,
        "fase": _stage(title),
        "wat": common_name(_clean(title))[:240],
        "datum": mantle.get("date") or (core.get("modified") or "")[:10] or None,
        "afstand_m": max(50, round(min(_distance(origin, point) for point in points) / 50) * 50),
        "door": common_name(", ".join(_value(core.get("creator")))) or None,
    }
    activities = _value(extra.get("activiteit"))
    if activities:
        item["activiteit"] = ", ".join(dict.fromkeys(activities))
    until = extra.get("datumEindeReactietermijn")
    if isinstance(until, str) and until:
        item["reageren_tot"] = until[:10]
    item["link"] = url
    return {key: value for key, value in item.items() if value is not None}


async def bekendmakingen(adres: str, plaats: str | None = None, straal_m: int = 250, weken: int = 8) -> dict[str, Any]:
    text, town = adres.strip(), (plaats or "").strip() or None
    if not town and "," in text:
        # "Rapenburg 70, Leiden": the town after the comma.
        text, town = (part.strip() for part in text.rsplit(",", 1))
    point = await _address(text, town)
    if not point:
        raise ToolError(
            f"Geen adres of straat gevonden die lijkt op '{adres}'. Noem een straat met huisnummer en de plaats, "
            "of een postcode."
        )
    radius = min(max(int(straal_m), MIN_RADIUS), MAX_RADIUS)
    since = (now_local() - timedelta(weeks=min(max(int(weken), 1), MAX_WEEKS))).date().isoformat()
    # KOOP only gets the point rounded to about a hundred metres, never the address itself.
    lat, lon = f"{point['lat']:.3f}", f"{point['lon']:.3f}"
    query = (
        f'c.product-area==officielepublicaties AND dt.modified>={since} AND w.locatiepunt within/etrs89 '
        f'"{lat} {lon} {(radius + 100) / 1000:.2f}" sortBy dt.modified/sort.descending'
    )
    params = {"query": query, "maximumRecords": MAX_RECORDS, "httpAccept": "application/json"}
    data = await cached(("bekendmakingen", query), TTL, lambda: get_json(SRU, params=params, source="KOOP"))
    records = (data.get("searchRetrieveResponse", {}).get("records") or {}).get("record") or []
    records = records if isinstance(records, list) else [records]

    found = [notice for record in records if (notice := _notice(record, point))]
    near = [notice for notice in found if notice["afstand_m"] <= radius]
    near.sort(key=lambda notice: (notice.get("datum") or "", -notice["afstand_m"]), reverse=True)
    # The place as a street and town, without the house number that was asked.
    where = common_name(re.sub(r"\s+\d+\S*(?=,|$)", "", POSTCODE.sub("", point["naam"])).strip(" ,"))
    result: dict[str, Any] = {"rond": where, "straal_m": radius, "sinds": since, "bekendmakingen": near[:MAX_ITEMS]}
    if len(near) > MAX_ITEMS:
        result["meer"] = f"Er zijn er {len(near)}; dit zijn de {MAX_ITEMS} nieuwste. Kies een kleinere straal of minder weken."
    if not near:
        result["uitleg"] = f"Geen bekendmakingen gevonden binnen {radius} meter sinds {since}."
    else:
        result["uitleg"] = UITLEG
    result["bron"] = BRON
    return result
