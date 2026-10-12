"""Places for the weather tools: a town, municipality, district or postcode, looked up at PDOK Locatieserver.

Never a street or an address: a street name without its town is in many places, and the weather needs no more
than the neighbourhood.
"""

import re
import unicodedata
from typing import NamedTuple

from mcp.server.mcpserver.exceptions import ToolError

from ..common import cached, get_json

LOCATIESERVER = "https://api.pdok.nl/bzk/locatieserver/search/v3_1/free"
AREAS = "type:(woonplaats OR gemeente OR wijk OR buurt)"
POSTCODES = "type:postcode"
FIELDS = "type,woonplaatsnaam,gemeentenaam,provincienaam,wijknaam,buurtnaam,centroide_ll"
OWN_NAME = {
    "woonplaats": "woonplaatsnaam", "gemeente": "gemeentenaam", "wijk": "wijknaam", "buurt": "buurtnaam",
    "postcode": "woonplaatsnaam",
}
POSTCODE = re.compile(r"\d{4}\s*[A-Za-z]{0,2}")
# PDOK ranks other places "aan Zee" above Katwijk for "Katwijk aan Zee", so that gets a second try as "Katwijk".
AAN = re.compile(r"\s+aan\s+(zee|den?\s+\S+)$", re.I)
GENERIC = {"station", "centraal", "centrum", "noord", "oost", "zuid", "west", "dorp", "stad", "haven", "strand", "buitengebied"}
# The names people use, rather than the official ones.
ALIASES = ((re.compile(r"'?s[- ]?gravenhage", re.I), "Den Haag"), (re.compile(r"'?s[- ]?hertogenbosch", re.I), "Den Bosch"))


class Plaats(NamedTuple):
    naam: str
    lat: float
    lon: float
    gemeente: str
    provincie: str


def _common(name: str) -> str:
    for pattern, alias in ALIASES:
        name = pattern.sub(alias, name)
    return name


def _words(text: str) -> set[str]:
    text = unicodedata.normalize("NFKD", _common(text)).encode("ascii", "ignore").decode().lower()
    return set(re.findall(r"[a-z0-9]+", text))


def _own_name(doc: dict) -> str:
    # Districts are called like "Wijk 07 Scheveningen".
    return re.sub(r"^Wijk \d+\s+", "", doc.get(OWN_NAME.get(doc.get("type"), ""), "") or "")


def _telling(doc: dict) -> set[str]:
    """The words of a hit's own name that tell it apart: a district called Station or Zuid is in many towns."""
    return {word for word in _words(_own_name(doc)) if len(word) >= 3 and word not in GENERIC}


def _pick(query: str, docs: list[dict]) -> dict | None:
    """The hit whose own name is in the question and that matches most of it, so "Station Leiden" is Leiden."""
    asked = _words(query)
    best, best_score = None, 0
    for doc in docs:
        telling = _telling(doc)
        if telling and telling <= asked:
            names = _words(_own_name(doc)) | _words(doc.get("woonplaatsnaam") or "") | _words(doc.get("gemeentenaam") or "")
            if len(asked & names) > best_score:
                best, best_score = doc, len(asked & names)
    return best


def _name(doc: dict) -> str:
    own = _common(_own_name(doc))
    town = _common(doc.get("woonplaatsnaam") or doc.get("gemeentenaam") or "")
    if doc.get("type") == "postcode":
        return town
    if doc.get("type") in ("wijk", "buurt") and town and town not in own:
        return f"{own}, {town}"
    return own


async def _search(query: str, types: str) -> list[dict]:
    params = {"q": query, "fq": types, "rows": 10, "fl": FIELDS}
    data = await cached(
        ("plaats", query.lower(), types), 24 * 3600,
        lambda: get_json(LOCATIESERVER, params=params, source="PDOK Locatieserver"),
    )
    return [doc for doc in data.get("response", {}).get("docs", []) if doc.get("centroide_ll", "").startswith("POINT(")]


async def zoek(plaats: str) -> Plaats:
    """Name, point, municipality and province of a place, at PDOK Locatieserver."""
    plaats = plaats.strip()
    if POSTCODE.fullmatch(plaats):
        docs = await _search(plaats, POSTCODES)
        doc = docs[0] if docs else None
    else:
        doc = _pick(plaats, await _search(plaats, AREAS))
        if not doc and AAN.search(plaats):
            shorter = AAN.sub("", plaats)
            doc = _pick(shorter, await _search(shorter, AREAS))
    if not doc:
        raise ToolError(f"Geen plaats gevonden die lijkt op '{plaats}'. Noem een plaats of wijk, bijvoorbeeld Leiden of Scheveningen.")
    lon, lat = doc["centroide_ll"][6:-1].split()
    return Plaats(_name(doc), float(lat), float(lon), doc.get("gemeentenaam") or "", doc.get("provincienaam") or "")
