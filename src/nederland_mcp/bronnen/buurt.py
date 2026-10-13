"""Figures about a buurt, wijk or gemeente from CBS: Kerncijfers wijken en buurten (CC BY 4.0, "Bron: CBS").

CBS fills each year's table in steps: the newest holds the population first, the housing figures come a year
later and incomes two years later. So every topic comes from the newest table that has it, with its year, and
with the gemeente and the Netherlands from that same table to compare. Averages over a whole area, never about one
house or person; CBS leaves out figures for small areas. Only neutral topics: no origin, benefits or youth care.
"""

import asyncio
import re
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from ..common import cached, get_json
from . import plaats as plaatsen

URL = "https://opendata.cbs.nl/ODataApi/odata/{table}/TypedDataSet"
# Year and table, newest first.
TABLES = ((2026, "86358NED"), (2025, "86165NED"), (2024, "85984NED"))
NETHERLANDS = "NL00"
BRON = "CBS, Kerncijfers wijken en buurten (CC BY 4.0)"
# The tables are renewed a few times a year.
TTL = 7 * 24 * 3600
NUMBER = re.compile(r"_\d+$")
URBANITY = {1: "zeer sterk stedelijk", 2: "sterk stedelijk", 3: "matig stedelijk", 4: "weinig stedelijk", 5: "niet stedelijk"}
EXPLAIN = (
    "Gemiddelden en percentages van het CBS voor het hele gebied, niet voor één huis. Elk onderdeel heeft het jaar "
    "van de cijfers: het CBS vult de nieuwste jaren in stappen. 'gemeente' en 'nederland' staan erbij om te "
    "vergelijken, uit hetzelfde jaar. Bij wonen is elk percentage van alle woningen: koop, huur van een "
    "woningcorporatie en huur van een andere verhuurder zijn samen ongeveer 100. Bij inkomen staat welk deel van de "
    "huishoudens bij de 40% laagste en bij de 20% hoogste inkomens van Nederland hoort, dus landelijk is dat 40 en "
    "20. Ontbreekt een cijfer, dan heeft het CBS het niet, bijvoorbeeld omdat het gebied te klein is."
)


def _pct(part: float | None, whole: float | None) -> int | None:
    return round(100 * part / whole) if part is not None and whole else None


def _thousands(value: float | None) -> int | None:
    """CBS gives money in thousands of euros."""
    return round(value * 1000) if value is not None else None


def _first(row: dict, *names: str) -> Any:
    """The first filled column of several names: CBS renames a few columns between years."""
    return next((row[name] for name in names if row.get(name) is not None), None)


def _filled(values: dict) -> dict:
    return {key: value for key, value in values.items() if value not in (None, {}, "")}


def _bevolking(row: dict) -> dict:
    people, households = row.get("AantalInwoners"), row.get("HuishoudensTotaal")
    old = row.get("k_65JaarOfOuder")
    if old is None and row.get("k_65JaarTot80Jaar") is not None:
        old = row["k_65JaarTot80Jaar"] + (row.get("k_80JaarOfOuder") or 0)
    return _filled({
        "inwoners": people,
        "inwoners_per_km2": row.get("Bevolkingsdichtheid"),
        "leeftijd_pct": _filled({
            "0-15": _pct(row.get("k_0Tot15Jaar"), people),
            "15-25": _pct(row.get("k_15Tot25Jaar"), people),
            "25-45": _pct(row.get("k_25Tot45Jaar"), people),
            "45-65": _pct(row.get("k_45Tot65Jaar"), people),
            "65+": _pct(old, people),
        }),
        "huishoudens": households,
        "gemiddelde_huishoudensgrootte": row.get("GemiddeldeHuishoudensgrootte"),
        "eenpersoonshuishoudens_pct": _pct(row.get("Eenpersoonshuishoudens"), households),
        "huishoudens_met_kinderen_pct": _pct(row.get("HuishoudensMetKinderen"), households),
    })


def _wonen(row: dict) -> dict:
    return _filled({
        "woningen": row.get("Woningvoorraad"),
        "gemiddelde_woz_waarde": _thousands(row.get("GemiddeldeWOZWaardeVanWoningen")),
        # Parts of all homes that add up to 100, so the corporations are not read as a part of the rented homes.
        "eigendom_pct": _filled({
            "koop": row.get("Koopwoningen"),
            "huur_van_woningcorporatie": row.get("InBezitWoningcorporatie"),
            "huur_van_andere_verhuurder": row.get("InBezitOverigeVerhuurders"),
        }),
        "eengezinswoningen_pct": row.get("PercentageEengezinswoning"),
        "appartementen_pct": row.get("PercentageMeergezinswoning"),
        "gebouwd_in_laatste_tien_jaar_pct": row.get("BouwjaarAfgelopenTienJaar"),
    })


def _inkomen(row: dict) -> dict:
    return _filled({
        "gemiddeld_inkomen_per_inwoner": _thousands(row.get("GemiddeldInkomenPerInwoner")),
        "huishoudens_in_laagste_40pct_inkomens_pct": row.get("k_40HuishoudensMetLaagsteInkomen"),
        "huishoudens_in_hoogste_20pct_inkomens_pct": row.get("k_20HuishoudensMetHoogsteInkomen"),
    })


def _energie(row: dict) -> dict:
    return _filled({
        "stroomverbruik_per_woning_kwh": _first(row, "GemiddeldeElektriciteitsleveringTotaal", "GemiddeldeElektriciteitslevering"),
        "gasverbruik_per_woning_m3": _first(row, "GemiddeldAardgasverbruikTotaal", "GemiddeldAardgasverbruik"),
        "woningen_met_zonnepanelen_pct": row.get("WoningenMetZonnestroom"),
        "aardgasvrije_woningen_pct": row.get("AardgasvrijeWoningen"),
    })


def _voorzieningen(row: dict) -> dict:
    return _filled({
        "afstand_huisarts_km": row.get("AfstandTotHuisartsenpraktijk"),
        "afstand_grote_supermarkt_km": row.get("AfstandTotGroteSupermarkt"),
        "afstand_kinderdagverblijf_km": row.get("AfstandTotKinderdagverblijf"),
        "afstand_school_km": row.get("AfstandTotSchool"),
        "scholen_binnen_3_km": row.get("ScholenBinnen3Km"),
    })


def _vervoer(row: dict) -> dict:
    return _filled({
        "autos_per_huishouden": row.get("PersonenautoSPerHuishouden"),
        "publieke_laadpalen": row.get("AantalPubliekeLaadpalen"),
    })


def _omgeving(row: dict) -> dict:
    urbanity = row.get("MateVanStedelijkheid")
    return _filled({
        "oppervlakte_land_ha": row.get("OppervlakteLand"),
        "oppervlakte_water_ha": row.get("OppervlakteWater"),
        "stedelijkheid": URBANITY.get(int(urbanity)) if str(urbanity or "").isdigit() else None,
    })


# Each topic, and the figures in it that are worth comparing with the gemeente and the Netherlands.
TOPICS = (
    ("bevolking", _bevolking, ("gemiddelde_huishoudensgrootte", "eenpersoonshuishoudens_pct")),
    ("wonen", _wonen, ("gemiddelde_woz_waarde", "eigendom_pct")),
    ("inkomen", _inkomen, ("gemiddeld_inkomen_per_inwoner", "huishoudens_in_laagste_40pct_inkomens_pct")),
    ("energie", _energie, ("stroomverbruik_per_woning_kwh", "gasverbruik_per_woning_m3")),
    ("voorzieningen", _voorzieningen, ("afstand_huisarts_km", "afstand_grote_supermarkt_km")),
    ("vervoer", _vervoer, ("autos_per_huishouden",)),
    ("omgeving", _omgeving, ()),
)


async def _table(table: str, codes: tuple[str, ...]) -> dict[str, dict]:
    """The rows of some areas in one year's table, by code, with column names without their number."""
    # CBS pads its codes with spaces to ten characters.
    wanted = " or ".join(f"WijkenEnBuurten eq '{code.ljust(10)}'" for code in codes)
    params = {"$format": "json", "$filter": wanted}
    data = await cached(
        ("cbs-kwb", table, codes), TTL, lambda: get_json(URL.format(table=table), params=params, source="CBS")
    )
    rows = {}
    for row in data.get("value") or []:
        clean = {NUMBER.sub("", key): value.strip() if isinstance(value, str) else value for key, value in row.items()}
        rows[clean.get("WijkenEnBuurten") or ""] = clean
    return rows


async def buurt_info(plaats: str) -> dict:
    if not plaats or not plaats.strip():
        raise ToolError("Noem een buurt, wijk, plaats, straat met plaats of postcode, bijvoorbeeld Vogelwijk Den Haag.")
    area = await plaatsen.gebied(plaats)
    codes = tuple(dict.fromkeys((area.code, area.gemeentecode, NETHERLANDS)))
    # A table that CBS is renewing can be closed for a few hours; the other years still answer.
    answers = await asyncio.gather(*(_table(table, codes) for _, table in TABLES), return_exceptions=True)
    for answer in answers:
        if isinstance(answer, BaseException) and not isinstance(answer, ToolError):
            raise answer
    years = [(year, rows) for (year, _), rows in zip(TABLES, answers) if isinstance(rows, dict) and rows]
    if not years:
        raise ToolError("De cijfers van het CBS zijn nu niet op te halen. Probeer het later nog eens.")

    result: dict[str, Any] = {"gebied": area.naam, "soort": area.soort}
    if area.wijk and area.wijk != area.naam:
        result["wijk"] = area.wijk
    if area.soort != "gemeente":
        result["gemeente"] = area.gemeente
    if area.via:
        result["gevonden_via"] = area.via
    if area.let_op:
        result["let_op"] = area.let_op
    found = False
    for name, topic, compared in TOPICS:
        for year, rows in years:
            figures = topic(rows.get(area.code) or {})
            if not figures:
                continue
            found = True
            section: dict[str, Any] = {"jaar": year, **figures}
            for label, code in (("gemeente", area.gemeentecode), ("nederland", NETHERLANDS)):
                if code == area.code:
                    continue
                other = topic(rows.get(code) or {})
                picked = {key: other[key] for key in compared if key in other}
                if picked:
                    section[label] = picked
            result[name] = section
            break
    if not found:
        raise ToolError(f"Het CBS heeft geen cijfers voor {area.naam}. Probeer de wijk of de gemeente.")
    result["uitleg"] = EXPLAIN
    result["bron"] = BRON
    return result
