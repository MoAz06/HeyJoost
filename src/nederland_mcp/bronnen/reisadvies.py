"""Travel advice per country from the Ministerie van Buitenlandse Zaken, through the open data API of
NederlandWereldwijd (CC0, no key).

The advice can change from one hour to the next during a crisis, so it is kept for half an hour and every answer
has the date of the last change and the link to the official page.
"""

import re
import unicodedata
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from ..common import cached, get_json, html_text, normalize

BASE = "https://opendata.nederlandwereldwijd.nl/v2/sources/nederlandwereldwijd/infotypes"
BRON = "Ministerie van Buitenlandse Zaken, reisadvies via NederlandWereldwijd (CC0)"
TTL = 30 * 60
COLOURS = ("groen", "geel", "oranje", "rood")
LEGEND = {
    "groen": "geen bijzondere veiligheidsrisico's",
    "geel": "let op, er zijn veiligheidsrisico's",
    "oranje": "alleen noodzakelijke reizen",
    "rood": "niet reizen",
}
def _key(name: str) -> str:
    """A name without accents, case or punctuation: België and Belgie are the same country."""
    plain = "".join(char for char in unicodedata.normalize("NFKD", name) if not unicodedata.combining(char))
    return normalize(plain)


# Places people name instead of the country.
ALIASES = {
    "amerika": "verenigde-staten-van-amerika", "vs": "verenigde-staten-van-amerika", "usa": "verenigde-staten-van-amerika",
    "verenigdestaten": "verenigde-staten-van-amerika", "newyork": "verenigde-staten-van-amerika",
    "florida": "verenigde-staten-van-amerika", "californie": "verenigde-staten-van-amerika",
    "engeland": "verenigd-koninkrijk", "schotland": "verenigd-koninkrijk", "wales": "verenigd-koninkrijk",
    "noordierland": "verenigd-koninkrijk", "uk": "verenigd-koninkrijk", "grootbrittannie": "verenigd-koninkrijk",
    "londen": "verenigd-koninkrijk", "dubai": "verenigde-arabische-emiraten", "abudhabi": "verenigde-arabische-emiraten",
    "vae": "verenigde-arabische-emiraten", "emiraten": "verenigde-arabische-emiraten",
    "bali": "indonesie", "lombok": "indonesie", "java": "indonesie", "phuket": "thailand", "bangkok": "thailand",
    "mallorca": "spanje", "ibiza": "spanje", "menorca": "spanje", "tenerife": "spanje", "grancanaria": "spanje",
    "lanzarote": "spanje", "fuerteventura": "spanje", "canarischeeilanden": "spanje", "barcelona": "spanje",
    "madrid": "spanje", "kreta": "griekenland", "rhodos": "griekenland", "kos": "griekenland", "corfu": "griekenland",
    "zakynthos": "griekenland", "athene": "griekenland", "antalya": "turkije", "istanbul": "turkije",
    "alanya": "turkije", "bodrum": "turkije", "hurghada": "egypte", "sharmelsheikh": "egypte", "cairo": "egypte", "marrakech": "marokko", "parijs": "frankrijk", "corsica": "frankrijk", "rome": "italie",
    "sicilie": "italie", "sardinie": "italie", "berlijn": "duitsland", "lissabon": "portugal", "madeira": "portugal",
    "algarve": "portugal", "azoren": "portugal", "tsjechischerepubliek": "tsjechie", "praag": "tsjechie",
    "witrusland": "belarus-wit-rusland", "belarus": "belarus-wit-rusland", "swaziland": "eswatini",
    "vaticaan": "vaticaanstad", "hongkong": "hongkong", "macau": "macau-sar", "zuidkorea": "zuid-korea",
    "korea": "zuid-korea", "noordkorea": "noord-korea", "palestina": "palestijnse-gebieden", "gaza": "palestijnse-gebieden",
    "westelijkejordaanoever": "palestijnse-gebieden", "kaapstad": "zuid-afrika", "congo": "congo-democratische-republiek",
    "svalbard": "spitsbergen", "tokio": "japan", "zanzibar": "tanzania",
}
# Words in a question that point to a part of the advice, by the start of its title.
TOPICS = {
    "visum": "paspoort", "paspoort": "paspoort", "id": "paspoort", "identiteitskaart": "paspoort",
    "rijbewijs": "paspoort", "documenten": "paspoort", "inreis": "paspoort", "esta": "paspoort", "eta": "paspoort",
    "vaccinatie": "reisvaccinaties", "vaccinaties": "reisvaccinaties", "inenting": "reisvaccinaties",
    "inentingen": "reisvaccinaties", "prik": "reisvaccinaties", "gezondheid": "reisvaccinaties",
    "malaria": "reisvaccinaties", "medicijnen": "medicijnen", "medicatie": "medicijnen", "bagage": "bagageregels",
    "verzekering": "reisverzekering", "reisverzekering": "reisverzekering", "terrorisme": "terrorisme",
    "aanslag": "terrorisme", "aanslagen": "terrorisme", "criminaliteit": "criminaliteit", "diefstal": "criminaliteit",
    "zakkenrollers": "criminaliteit", "beroving": "criminaliteit", "oplichting": "criminaliteit",
    "wetten": "wetten en gebruiken", "gebruiken": "wetten en gebruiken", "drugs": "wetten en gebruiken",
    "alcohol": "wetten en gebruiken", "kleding": "wetten en gebruiken", "lhbti": "wetten en gebruiken",
    "homo": "wetten en gebruiken", "ramadan": "wetten en gebruiken", "aardbeving": "natuurgeweld",
    "aardbevingen": "natuurgeweld", "orkaan": "natuurgeweld", "orkanen": "natuurgeweld", "vulkaan": "natuurgeweld",
    "bosbrand": "natuurgeweld", "bosbranden": "natuurgeweld", "natuurgeweld": "natuurgeweld", "hitte": "natuurgeweld",
    "overstroming": "natuurgeweld", "demonstratie": "demonstraties", "demonstraties": "demonstraties",
    "protest": "demonstraties", "verkeer": "verkeersongevallen", "autorijden": "verkeersongevallen",
    "nood": "in geval van nood", "noodgeval": "in geval van nood", "ambassade": "in geval van nood",
    "consulaat": "in geval van nood", "hulp": "in geval van nood", "veilig": "regionale risico",
    "veiligheid": "regionale risico", "gevaarlijk": "regionale risico", "regio": "regionale risico",
    "kleurcode": "regionale risico",
}
# Looked up by _key, like the names they are compared with.
ALIASES = {_key(name): country for name, country in ALIASES.items()}
TOPICS = {_key(word): topic for word, topic in TOPICS.items()}
MAX_DETAIL = 3000


async def _countries() -> dict[str, dict]:
    """Every country with travel advice, by its normalized name, key and ISO code."""

    async def fetch() -> list[dict]:
        rows: list[dict] = []
        for offset in (0, 200):
            page = await get_json(
                f"{BASE}/countries", params={"output": "json", "rows": 200, "offset": offset}, source="NederlandWereldwijd"
            )
            rows += page if isinstance(page, list) else []
        return rows

    countries: dict[str, dict] = {}
    for row in await cached(("reisadvies-landen",), 24 * 3600, fetch):
        for name in (row.get("location"), row.get("locationkey"), row.get("isocode")):
            if name:
                countries.setdefault(_key(name), row)
    return countries


async def _country(land: str) -> dict:
    countries = await _countries()
    key = _key(land)
    found = countries.get(_key(ALIASES.get(key, key)))
    if not found:
        # "Spanje (Mallorca)" or "het zuiden van Turkije": try each word.
        words = (_key(word) for word in re.split(r"[\s(),/]+", land))
        found = next((countries[_key(ALIASES.get(word, word))] for word in words if _key(ALIASES.get(word, word)) in countries), None)
    if not found:
        raise ToolError(
            f"Geen reisadvies gevonden voor '{land}'. Noem het land in het Nederlands, bijvoorbeeld Turkije, Spanje "
            "of Verenigde Staten. Voor Nederland zelf is er geen reisadvies."
        )
    return found


def _topics(onderwerp: str | None) -> set[str]:
    words = {_key(word) for word in re.split(r"\W+", onderwerp or "") if word}
    return {TOPICS[word] for word in words if word in TOPICS} | {word for word in words if len(word) >= 5}


async def reisadvies(land: str, onderwerp: str | None = None) -> dict[str, Any]:
    country = await _country(land)
    code = country["isocode"].lower()
    url = f"{BASE}/countries/{code}/traveladvice"
    try:
        advice = await cached(("reisadvies", code), TTL, lambda: get_json(url, params={"output": "json"}, source="NederlandWereldwijd"))
    except ToolError as exc:
        if "HTTP 404" in str(exc):
            raise ToolError(f"Voor {country['location']} heeft Buitenlandse Zaken geen reisadvies.") from exc
        raise
    if isinstance(advice, list):
        advice = advice[0] if advice else {}
    if not advice:
        raise ToolError(f"Voor {country['location']} heeft Buitenlandse Zaken geen reisadvies.")

    intro = advice.get("introduction") or ""
    summary = re.sub(r'<div class="notification.*?</div>', "", intro, flags=re.S)
    in_short = [line.removeprefix("- ") for line in html_text(summary).splitlines() if line.lower() != "in het kort"]
    notes = [
        html_text(note) for note in re.findall(r'<div class="notification[^"]*">(.*?)</div>', intro, re.S)
        if "informatieservice" not in note.lower()
    ]
    colours = [colour for colour in COLOURS if re.search(rf"\b{colour}\b", " ".join(in_short).lower())]

    sections = [
        (block.get("paragraphtitle") or "", block.get("paragraph") or "")
        for part in advice.get("content") or [] for block in part.get("contentblocks") or []
    ]
    result: dict[str, Any] = {"land": advice.get("location") or country["location"]}
    if colours:
        result["kleurcodes"] = {colour: LEGEND[colour] for colour in reversed(colours)}
    result["in_het_kort"] = in_short
    if notes:
        result["let_op"] = notes
    modified = advice.get("lastmodified") or ""
    if modified:
        result["laatst_gewijzigd"] = modified[:10]
    still = re.search(r"Nog steeds geldig op: ([\d-]+)", advice.get("modificationdate") or "")
    if still:
        result["nog_geldig_op"] = still[1]

    wanted = _topics(onderwerp)
    if wanted:
        details, size = [], 0
        for title, body in sections:
            name = title.lower()
            text = html_text(body)
            if any(name.startswith(topic) for topic in wanted) or any(topic in _key(title) for topic in wanted):
                details.append({"onderwerp": title, "tekst": text[: max(MAX_DETAIL - size, 400)]})
                size += len(text)
            if size >= MAX_DETAIL:
                break
        if details:
            result["details"] = details
    result["onderwerpen"] = [title for title, _ in sections if title]
    result["link"] = advice.get("canonical") or f"https://www.nederlandwereldwijd.nl/reisadvies/{country.get('locationkey')}"
    result["bron"] = BRON
    return result
