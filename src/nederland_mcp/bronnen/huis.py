"""Address and building data: PDOK Locatieserver, BAG (Kadaster) and EP-online (RVO)."""

import os
import re
from typing import Any

import httpx
from mcp.server.mcpserver.exceptions import ToolError

from ..common import cached, client, get_json

LOCATIESERVER = "https://api.pdok.nl/bzk/locatieserver/search/v3_1/free"
BAG = "https://api.pdok.nl/kadaster/bag/ogc/v2/collections"
EP_ONLINE = "https://public.ep-online.nl/api/v5/PandEnergielabel/AdresseerbaarObject/{id}"

DAY = 24 * 3600
EP_FIELDS = ("Energieklasse", "Registratiedatum", "Geldig_tot", "Opnamedatum", "Gebouwtype", "Gebouwsubtype")


def _postcode(value: str) -> str:
    postcode = re.sub(r"\s", "", value).upper()
    if not re.fullmatch(r"\d{4}[A-Z]{2}", postcode):
        raise ToolError(f"'{value}' is geen geldige postcode. Gebruik vier cijfers en twee letters, bijvoorbeeld 2511CV.")
    return postcode


async def _find_address(postcode: str, huisnummer: int, toevoeging: str | None) -> dict:
    params = [
        ("q", "*:*"),
        ("fq", "type:adres"),
        ("fq", f"postcode:{postcode}"),
        ("fq", f"huisnummer:{huisnummer}"),
        ("rows", "50"),
    ]
    data = await cached(
        ("locatieserver", postcode, huisnummer),
        DAY,
        lambda: get_json(LOCATIESERVER, params=params, source="PDOK Locatieserver"),
    )
    docs = data.get("response", {}).get("docs", [])
    if not docs:
        raise ToolError(f"Geen adres gevonden voor {postcode} {huisnummer}.")

    wanted = f"{huisnummer}{toevoeging or ''}".replace("-", "").replace(" ", "").upper()
    for doc in docs:
        if str(doc.get("huis_nlt", "")).replace("-", "").upper() == wanted:
            return doc
    options = ", ".join(sorted(str(d.get("huis_nlt")) for d in docs))
    if toevoeging:
        raise ToolError(f"Toevoeging '{toevoeging}' niet gevonden. Bestaande huisnummers op dit adres: {options}.")
    if len(docs) > 1:
        raise ToolError(f"Op {postcode} {huisnummer} zitten meerdere adressen: {options}. Geef de toevoeging mee.")
    return docs[0]


async def _bag_item(collection: str, query: dict | None = None, item_id: str | None = None) -> dict | None:
    url = f"{BAG}/{collection}/items" + (f"/{item_id}" if item_id else "")
    params = {"f": "json", **(query or {})}
    data = await cached(
        ("bag", collection, item_id, tuple(sorted(params.items()))),
        DAY,
        lambda: get_json(url, params=params, source="BAG (PDOK)"),
    )
    if item_id:
        return data.get("properties")
    features = data.get("features", [])
    return features[0]["properties"] if features else None


async def _energy_label(object_id: str) -> dict | str:
    key = os.environ.get("EP_ONLINE_API_KEY")
    if not key:
        return "Niet beschikbaar: de server heeft (nog) geen EP-online API-key."

    async def fetch() -> Any:
        response = await client().get(
            EP_ONLINE.format(id=object_id),
            headers={"Authorization": key, "Accept": "application/json"},
        )
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise ToolError(f"EP-online gaf een fout (HTTP {response.status_code}).")
        return response.json()

    try:
        data = await cached(("ep-online", object_id), DAY, fetch)
    except httpx.HTTPError:
        return "Energielabel tijdelijk niet beschikbaar: EP-online is nu niet bereikbaar."
    except ToolError as exc:
        return f"Energielabel tijdelijk niet beschikbaar: {exc}"
    if not data:
        return "Geen geregistreerd energielabel gevonden voor dit adres."
    label = data[0] if isinstance(data, list) else data
    if not isinstance(label, dict):
        return "Geen geregistreerd energielabel gevonden voor dit adres."
    picked = {k: label[k] for k in EP_FIELDS if label.get(k) is not None}
    if not picked:
        picked = {k: v for k, v in label.items() if isinstance(v, (str, int, float)) and v not in ("", None)}
    return picked


async def huis_info(postcode: str, huisnummer: int, toevoeging: str | None = None) -> dict:
    postcode = _postcode(postcode)
    address = await _find_address(postcode, huisnummer, toevoeging)
    result: dict[str, Any] = {
        "adres": address.get("weergavenaam"),
        "gemeente": address.get("gemeentenaam"),
        "wijk": address.get("wijknaam"),
        "buurt": address.get("buurtnaam"),
        "provincie": address.get("provincienaam"),
    }

    object_id = address.get("adresseerbaarobject_id")
    if object_id:
        unit = await _bag_item("verblijfsobject", {"identificatie": object_id})
        if unit:
            result["oppervlakte_m2"] = unit.get("oppervlakte")
            result["gebruiksdoel"] = unit.get("gebruiksdoel")
            result["status"] = unit.get("status")
            pand_links = unit.get("pand.href") or []
            if pand_links:
                pand = await _bag_item("pand", item_id=pand_links[0].rstrip("/").split("/")[-1])
                if pand:
                    result["bouwjaar"] = pand.get("bouwjaar")
                    result["adressen_in_pand"] = pand.get("aantal_verblijfsobjecten")
        result["energielabel"] = await _energy_label(object_id)

    result["bron"] = "BAG via PDOK (Kadaster, publiek domein); energielabel: EP-online (RVO)"
    return result
