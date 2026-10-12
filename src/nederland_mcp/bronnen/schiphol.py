"""Schiphol Public Flight API v4, only for the chat on heyjoost.nl.

Schiphol does not allow sharing this API as an MCP source, so it is not an MCP tool. Answering in the chat on
our own website is allowed, with the limits we agreed on: one flight per lookup, only yesterday, today and
tomorrow, no lists and no history, a limit per visitor and for the whole site (in vraag.py), Schiphol named as
the source in every answer, and nothing cached longer than a minute.

Needs SCHIPHOL_CLIENT_ID and SCHIPHOL_CLIENT_SECRET from an application on developer.schiphol.nl.
They are exchanged for a short-lived token (OAuth 2.0 client credentials via Auth0).
"""

import os
import re
import time
from datetime import date

from mcp.server.mcpserver.exceptions import ToolError

from ..common import cached, fmt_datetime, get_json, now_local, parse_time, post_form

BASE = "https://api.schiphol.nl/public/public-flights/v4"
TOKEN_URL = "https://api.auth.schiphol.nl/oauth/token"
AUDIENCE = "https://api.schiphol.nl/public"
SOURCE = "Schiphol Flight API"

_token: tuple[str, float] | None = None  # (access token, monotonic time after which we fetch a new one)

STATES = {
    "SCH": "volgens planning",
    "DEL": "vertraagd",
    "WIL": "wacht in de lounge",
    "GTO": "gate open",
    "BRD": "aan het boarden",
    "GCL": "gate sluit",
    "GTD": "gate gesloten",
    "DEP": "vertrokken",
    "CNX": "geannuleerd",
    "GCH": "gatewijziging",
    "TOM": "morgen",
    "AIR": "in de lucht",
    "EXP": "landing verwacht",
    "FIR": "in het Nederlandse luchtruim",
    "LND": "geland",
    "FIB": "eerste bagage op de band",
    "ARR": "aangekomen",
    "DIV": "omgeleid",
}


def available() -> bool:
    return bool(os.environ.get("SCHIPHOL_CLIENT_ID") and os.environ.get("SCHIPHOL_CLIENT_SECRET"))


def _flight_name(value: str) -> str:
    match = re.fullmatch(r"([A-Z0-9]{2})(\d{1,4})", re.sub(r"\s", "", value).upper())
    if not match:
        raise ToolError(f"'{value}' lijkt geen vluchtnummer. Gebruik bijvoorbeeld KL1234 of HV5131.")
    return f"{match.group(1)}{int(match.group(2))}"


def flight(vluchtnummer: str, datum: str | None = None) -> tuple[str, str]:
    """The flight name and day (JJJJ-MM-DD) asked for. Only yesterday, today and tomorrow: no history."""
    name = _flight_name(vluchtnummer)
    today = now_local().date()
    try:
        day = date.fromisoformat(datum.strip()) if datum else today
    except ValueError as exc:
        raise ToolError(f"Gebruik de datum als JJJJ-MM-DD, bijvoorbeeld {today.isoformat()}.") from exc
    if abs((day - today).days) > 1:
        raise ToolError("Vluchten kan ik alleen opzoeken voor gisteren, vandaag en morgen.")
    return name, day.isoformat()


async def _headers() -> dict:
    global _token
    if not _token or _token[1] <= time.monotonic():
        client_id, secret = os.environ.get("SCHIPHOL_CLIENT_ID"), os.environ.get("SCHIPHOL_CLIENT_SECRET")
        if not client_id or not secret:
            raise ToolError("Vluchtinformatie is nog niet beschikbaar: de server heeft geen Schiphol API-key.")
        form = {"grant_type": "client_credentials", "client_id": client_id, "client_secret": secret, "audience": AUDIENCE}
        data = await post_form(TOKEN_URL, form, source="Schiphol inlogdienst")
        if not data.get("access_token"):
            raise ToolError("Schiphol gaf geen toegangstoken terug. Probeer het zo nog eens.")
        # Tokens live 30 minutes; renew a minute early so no request goes out with an expired one.
        _token = (data["access_token"], time.monotonic() + int(data.get("expires_in", 1800)) - 60)
    return {"Accept": "application/json", "Authorization": f"Bearer {_token[0]}"}


async def vlucht_info(vluchtnummer: str, datum: str | None = None) -> dict:
    name, day = flight(vluchtnummer, datum)
    params = {"flightName": name, "scheduleDate": day}

    async def fetch() -> dict:
        return await get_json(f"{BASE}/flights", params=params, headers=await _headers(), source=SOURCE)

    data = await cached(("schiphol", name, day), 60, fetch)
    flights = data.get("flights") or []
    if not flights:
        raise ToolError(f"Geen vlucht {name} gevonden op Schiphol op {day}.")

    # One flight: a flight number that lands and leaves again the same day has two legs, one per direction.
    legs = {}
    for item in flights:
        legs.setdefault(item.get("flightDirection"), item)
    results = []
    for leg in legs.values():
        departing = leg.get("flightDirection") == "D"
        states = (leg.get("publicFlightState") or {}).get("flightStates") or []
        airports = (leg.get("route") or {}).get("destinations")
        results.append({
            "vlucht": leg.get("flightName"),
            "richting": "vertrek vanaf Schiphol" if departing else "aankomst op Schiphol",
            "naar_iata" if departing else "van_iata": airports,
            "gepland": fmt_datetime(parse_time(leg.get("scheduleDateTime"))),
            "verwacht": fmt_datetime(parse_time(
                leg.get("publicEstimatedOffBlockTime") if departing else leg.get("estimatedLandingTime")
            )),
            "werkelijk": fmt_datetime(parse_time(
                leg.get("actualOffBlockTime") if departing else leg.get("actualLandingTime")
            )),
            "status": [STATES.get(s, s) for s in states],
            "terminal": leg.get("terminal"),
            "gate": leg.get("gate"),
            "bagageband": (leg.get("baggageClaim") or {}).get("belts"),
        })
    return {"vluchten": results, "bron": "Schiphol"}
