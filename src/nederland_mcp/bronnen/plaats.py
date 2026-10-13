"""Places, looked up at PDOK Locatieserver: a town, municipality, district or postcode for the weather tools, and
the buurt, wijk or gemeente of a place, street or postcode for the CBS figures.

The weather never takes a street or an address: a street name without its town is in many places, and the weather
needs no more than the neighbourhood. The CBS figures take a street only with its town.
"""

import asyncio
import re
import unicodedata
from typing import NamedTuple

from mcp.server.mcpserver.exceptions import ToolError

from ..common import cached, get_json, place_key

LOCATIESERVER = "https://api.pdok.nl/bzk/locatieserver/search/v3_1/free"
REVERSE = "https://api.pdok.nl/bzk/locatieserver/search/v3_1/reverse"
AREAS = "type:(woonplaats OR gemeente OR wijk OR buurt)"
STREETS = "type:weg"
DISTRICTS = "type:wijk"
POSTCODES = "type:postcode"
FIELDS = (
    "type,woonplaatsnaam,gemeentenaam,provincienaam,wijknaam,buurtnaam,straatnaam,centroide_ll,buurtcode,wijkcode,"
    "gemeentecode,weergavenaam"
)
OWN_NAME = {
    "woonplaats": "woonplaatsnaam", "gemeente": "gemeentenaam", "wijk": "wijknaam", "buurt": "buurtnaam",
    "postcode": "woonplaatsnaam", "weg": "straatnaam",
}
POSTCODE = re.compile(r"\d{4}\s*[A-Za-z]{0,2}")
# PDOK ranks other places "aan Zee" above Katwijk for "Katwijk aan Zee", so that gets a second try as "Katwijk".
AAN = re.compile(r"\s+aan\s+(zee|den?\s+\S+)$", re.I)
GENERIC = {"station", "centraal", "centrum", "noord", "oost", "zuid", "west", "dorp", "stad", "haven", "strand", "buitengebied"}
# Words in town names that do not tell one town from another, like in Den Helder or Bergen (NH).
TOWN_FILLERS = {"aan", "bij", "de", "den", "der", "en", "het", "in", "op", "gn", "nh", "zh", "l", "o"}
# The province after the name of a town that is in more provinces, like Haren Gn or Bergen (NH).
PROVINCES = {"dr", "fl", "fr", "gld", "gn", "l", "lb", "nb", "nh", "o", "ov", "u", "z", "zh", "zl"}
# People often end a district name otherwise than CBS does: the Schilderswijk in Den Haag is Schildersbuurt there.
ENDINGS = ("wijk", "buurt", "kwartier")
# The names people use, rather than the official ones.
ALIASES = ((re.compile(r"'?s[- ]?gravenhage", re.I), "Den Haag"), (re.compile(r"'?s[- ]?hertogenbosch", re.I), "Den Bosch"))


class Plaats(NamedTuple):
    naam: str
    lat: float
    lon: float
    gemeente: str
    provincie: str


class Gebied(NamedTuple):
    soort: str  # buurt, wijk or gemeente: the levels CBS has figures for
    code: str  # the CBS code, like BU05460000, WK054600 or GM0546
    naam: str
    gemeente: str
    gemeentecode: str  # like GM0546
    wijk: str | None = None  # the wijk of a buurt
    via: str | None = None  # the street or postcode it was found by
    let_op: str | None = None  # when the figures are not exactly about what was asked


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


def _names(doc: dict) -> set[str]:
    """All the words of a hit: its own name, its town and its gemeente."""
    return _words(_own_name(doc)) | _words(doc.get("woonplaatsnaam") or "") | _words(doc.get("gemeentenaam") or "")


def _pick(query: str, docs: list[dict]) -> dict | None:
    """The hit whose own name is in the question and that matches most of it, so "Station Leiden" is Leiden."""
    asked = _words(query)
    best, best_score = None, 0
    for doc in docs:
        telling = _telling(doc)
        if telling and telling <= asked:
            names = _names(doc)
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


async def _buurt_at(doc: dict) -> dict:
    """The buurt at the middle point of a street or postcode."""
    lon, lat = doc["centroide_ll"][6:-1].split()
    params = {"lat": lat, "lon": lon, "type": "buurt", "rows": 1, "fl": FIELDS}
    data = await cached(
        ("plaats-buurt", lat, lon), 24 * 3600,
        lambda: get_json(REVERSE, params=params, source="PDOK Locatieserver"),
    )
    docs = [d for d in data.get("response", {}).get("docs", []) if d.get("buurtcode") and d.get("gemeentecode")]
    if not docs:
        raise ToolError("Op die plek vond ik geen buurt. Noem de buurt, wijk of plaats.")
    return docs[0]


def _as_buurt(doc: dict, via: str | None = None) -> Gebied:
    wijk = _common(re.sub(r"^Wijk \d+\s+", "", doc.get("wijknaam") or "")) or None
    return Gebied(
        "buurt", doc["buurtcode"], _common(doc.get("buurtnaam") or ""), _common(doc.get("gemeentenaam") or ""),
        "GM" + doc["gemeentecode"], wijk, via,
    )


def _as_wijk(doc: dict) -> Gebied:
    return Gebied(
        "wijk", doc["wijkcode"], _common(_own_name(doc)), _common(doc.get("gemeentenaam") or ""),
        "GM" + doc["gemeentecode"],
    )


def _bare(town: str) -> str:
    """A town name without its province, like Haren for Haren Gn, but Wijk aan Zee stays whole."""
    words = town.split()
    while len(words) > 1 and words[-1].strip("()").lower() in PROVINCES:
        words.pop()
    return " ".join(words)


async def _town_districts(town: str, gemeentecode: str) -> list[dict]:
    """The wijken named after a town in a larger gemeente, like Voorhout Oude Dorp in Teylingen."""
    start = place_key(_bare(town))
    docs = await _search(_bare(town), DISTRICTS)
    return [
        d for d in docs
        if d.get("gemeentecode") == gemeentecode and d.get("wijkcode") and place_key(_own_name(d)).startswith(start)
    ]


def _with_town(docs: list[dict], query: str) -> list[dict]:
    """Leaves out streets whose town is not in the question: Aan Zee in Vollenhove is not Katwijk aan Zee, and the
    Hoofdstraat in Den Helder is not the one in Den Bosch."""
    asked = _words(query)
    return [
        d for d in docs
        if d.get("type") != "weg" or (_words(d.get("woonplaatsnaam") or "") - TOWN_FILLERS) & asked
    ]


async def _areas_and_streets(query: str) -> list[dict]:
    """Areas before streets, so a name that is both, like Vogelwijk, is the area. Apart, so streets cannot push
    the areas out of the first ten."""
    areas, streets = await asyncio.gather(_search(query, AREAS), _search(query, STREETS))
    return areas + streets


def _explained(doc: dict) -> set[str]:
    """The words of a question that a hit accounts for: its names, its province and words that tell nothing."""
    return _names(doc) | _words(doc.get("provincienaam") or "") | GENERIC | TOWN_FILLERS


def _endings(word: str) -> list[str]:
    """A word as asked and with the other district endings, like Schildersbuurt for Schilderswijk and
    Transvaalkwartier for Transvaal."""
    ending = next((e for e in ENDINGS if word.endswith(e) and len(word) > len(e) + 2), "")
    stem = word[: len(word) - len(ending)]
    return [word] + [stem + e for e in ENDINGS if e != ending]


async def _inside(gemeentecode: str, words: set[str]) -> Gebied | None:
    """The wijk or buurt in a gemeente that the other words of the question name, as CBS names it."""
    within = f"type:(wijk OR buurt) AND gemeentecode:{gemeentecode}"
    for word in sorted(w for w in words if w.isalpha() and len(w) >= 4)[:2]:
        for name in _endings(word):
            hits = [
                d for d in await _search(name, within)
                if name in _words(_own_name(d)) and d.get("wijkcode" if d.get("type") == "wijk" else "buurtcode")
            ]
            if not hits:
                continue
            # The area with exactly that name first, and a wijk before its buurten.
            hit = min(hits, key=lambda d: (place_key(_own_name(d)) != place_key(name), d.get("type") != "wijk"))
            area = _as_wijk(hit) if hit.get("type") == "wijk" else _as_buurt(hit)
            return area if name == word else area._replace(let_op=f"Bij het CBS heet dit gebied {area.naam}.")
    return None


async def _area(doc: dict) -> Gebied:
    """The CBS area of a hit: a street becomes the buurt at its middle, a town the gemeente or its own wijk."""
    kind = doc.get("type")
    if kind == "weg":
        street, town = doc.get("straatnaam") or "", _common(doc.get("woonplaatsnaam") or "")
        return _as_buurt(await _buurt_at(doc), via=f"{street}, {town}: de buurt bij het midden van de straat")
    if kind == "buurt" and doc.get("buurtcode"):
        return _as_buurt(doc)
    if kind == "wijk" and doc.get("wijkcode"):
        return _as_wijk(doc)
    gemeente, code = _common(doc.get("gemeentenaam") or ""), "GM" + doc["gemeentecode"]
    town = _common(doc.get("woonplaatsnaam") or "")
    if kind != "woonplaats" or place_key(town) == place_key(gemeente):
        return Gebied("gemeente", code, gemeente, gemeente, code)
    # CBS counts per gemeente, wijk and buurt, not per town: a town in a larger gemeente gets its wijk with the
    # same name, or else the gemeente with the wijken named after the town.
    districts = await _town_districts(town, doc["gemeentecode"])
    same = [d for d in districts if place_key(_own_name(d)) == place_key(_bare(town))]
    if same:
        return _as_wijk(same[0])
    town = _bare(town)
    note = f"{town} ligt in de gemeente {gemeente}, en het CBS telt per gemeente, wijk en buurt."
    names = [_own_name(d) for d in districts]
    if len(names) > 1:
        note += f" De wijken van {town} zijn {', '.join(names[:-1])} en {names[-1]}".rstrip(".") + "."
    elif names:
        note += f" De wijk van {town} is {names[0]}".rstrip(".") + "."
    return Gebied("gemeente", code, gemeente, gemeente, code, let_op=note)


async def gebied(plaats: str) -> Gebied:
    """The buurt, wijk or gemeente of a place, street or postcode, with its CBS code."""
    plaats = plaats.strip()
    if POSTCODE.fullmatch(plaats):
        docs = await _search(plaats, POSTCODES)
        if not docs:
            raise ToolError(f"Postcode {plaats} vond ik niet. Noem de buurt, wijk of plaats.")
        via = docs[0].get("weergavenaam") or plaats
        if not re.search(r"[A-Za-z]", plaats):
            via += f": de buurt bij één adres, want postcode {plaats} beslaat meestal meer buurten"
        return _as_buurt(await _buurt_at(docs[0]), via=via)

    docs = await _areas_and_streets(plaats)
    asked = plaats
    doc = _pick(asked, _with_town(docs, asked))
    if not doc and AAN.search(plaats):
        asked = AAN.sub("", plaats)
        doc = _pick(asked, _with_town(await _areas_and_streets(asked), asked))
    if not doc or not doc.get("gemeentecode"):
        streets = [d for d in docs if d.get("type") == "weg" and _telling(d) and _telling(d) <= _words(plaats)]
        if streets:
            towns = ", ".join(dict.fromkeys(_common(d.get("woonplaatsnaam") or "") for d in streets[:4]))
            raise ToolError(f"Noem de plaats bij de straat: {streets[0].get('straatnaam')} is er in onder meer {towns}.")
        raise ToolError(
            f"Geen buurt, wijk of plaats gevonden die lijkt op '{plaats}'. Noem bijvoorbeeld Vogelwijk Den Haag, "
            "Rapenburg Leiden of een postcode."
        )
    # Words of the question that the hit does not explain, like a district that CBS names otherwise or a street that
    # is not there.
    unexplained = _words(asked) - _explained(doc)
    if unexplained and doc.get("type") in ("gemeente", "woonplaats"):
        inside = await _inside(doc["gemeentecode"], unexplained)
        if inside:
            return inside
    area = await _area(doc)
    if not area.let_op and unexplained:
        area = area._replace(let_op=f"'{plaats}' vond ik niet precies, dus dit zijn de cijfers van {area.naam}.")
    return area
