"""Live check of every tool against the real sources.

Run from the project folder: uv run python scripts/smoke_test.py
Tools that need an API-key you have not set are reported as skipped, not failed.
"""

import asyncio
import json
import sys
from datetime import timedelta

from dotenv import find_dotenv, load_dotenv
from mcp.server.mcpserver.exceptions import ToolError

from nederland_mcp.bronnen import schiphol
from nederland_mcp.common import now_local
from nederland_mcp.server import server

SOON = now_local() + timedelta(minutes=30)
TOMORROW = (now_local() + timedelta(days=1)).date().isoformat()
NEXT_WEEK = (now_local() + timedelta(days=7)).date().isoformat()
YESTERDAY = (now_local() - timedelta(days=1)).date().isoformat()

CALLS = [
    ("huis_info", {"postcode": "2511 cv", "huisnummer": 3}),
    ("huis_info", {"straat": "Rapenburg", "huisnummer": 70, "plaats": "Leiden"}),
    ("huis_info", {"straat": "Spui", "huisnummer": 70, "plaats": "Den Haag"}),
    ("huis_info", {"straat": "Rapenburg", "huisnummer": 70}),
    ("huis_info", {"postcode": "9999ZZ", "huisnummer": 1}),
    ("buurt_info", {"plaats": "Vogelwijk Den Haag"}),
    ("buurt_info", {"plaats": "Rapenburg Leiden"}),
    ("buurt_info", {"plaats": "2311EZ"}),
    ("buurt_info", {"plaats": "Voorhout"}),
    ("buurt_info", {"plaats": "Xyzabc"}),
    ("schoolvakanties", {"regio": "zuid"}),
    ("schoolvakanties", {"schooljaar": "2026-2027", "regio": "Noord"}),
    ("reisadvies", {"land": "Turkije"}),
    ("reisadvies", {"land": "Mallorca", "onderwerp": "visum"}),
    ("reisadvies", {"land": "Xyzabc"}),
    ("regelzaken", {"vraag": "huurtoeslag aanvragen"}),
    ("regelzaken", {"vraag": "paspoort verlengen", "gemeente": "Leiden"}),
    ("regelzaken", {"vraag": "trouwen", "gemeente": "Rotterdam"}),
    ("bekendmakingen", {"adres": "Rapenburg 70", "plaats": "Leiden"}),
    ("bekendmakingen", {"adres": "Laan van Meerdervoort", "plaats": "Den Haag", "straal_m": 150}),
    ("bekendmakingen", {"adres": "Xyzstraat 1, Xyzabc"}),
    ("verkeer", {}),
    ("verkeer", {"weg": "A12"}),
    ("verkeer", {"waar": "Utrecht", "straal_km": 5}),
    ("verkeer", {"weg": "Q99"}),
    ("open_in_de_buurt", {"waar": "Rapenburg 70", "plaats": "Leiden"}),
    ("open_in_de_buurt", {"waar": "Utrecht Centraal", "soort": "apotheek"}),
    ("open_in_de_buurt", {"waar": "Scheveningen", "soort": "pinautomaat", "nu_open": True, "aantal": 3}),
    ("open_in_de_buurt", {"waar": "Leiden", "soort": "bakker"}),
    ("reis_plannen", {"van": "Rapenburg 70", "van_plaats": "Leiden", "naar": "Kurhaus", "naar_plaats": "Scheveningen"}),
    ("water_actueel", {"plaats": "Hoek van Holland"}),
    ("water_actueel", {"plaats": "Den Haag", "soort": "waterstand"}),
    ("water_actueel", {"plaats": "Scheveningen", "soort": "watertemperatuur"}),
    ("water_actueel", {"plaats": "Lobith", "soort": "waterstand"}),
    ("water_verwachting", {"plaats": "Scheveningen"}),
    ("water_verwachting", {"plaats": "Harlingen", "datum": TOMORROW}),
    ("water_verwachting", {"plaats": "Vlissingen", "datum": NEXT_WEEK}),
    ("water_verwachting", {"plaats": "Venlo"}),
    ("weer", {"plaats": "Leiden"}),
    ("weer", {"plaats": "Scheveningen", "datum": TOMORROW}),
    ("weer", {"plaats": "Katwijk aan Zee"}),
    ("weer", {"plaats": "2312"}),
    ("weer", {"plaats": "Leiden", "datum": NEXT_WEEK}),
    ("weer", {"plaats": "Xyzabc"}),
    ("regen", {"plaats": "Leiden"}),
    ("regen", {"plaats": "Scheveningen"}),
    ("weerwaarschuwingen", {}),
    ("weerwaarschuwingen", {"plaats": "Groningen"}),
    ("weerwaarschuwingen", {"plaats": "Zeeland"}),
    ("stroomprijs", {}),
    ("stroomprijs", {"datum": YESTERDAY}),
    ("stroomprijs", {"datum": "2020-01-01"}),
    ("trein_vertrek", {"station": "Leiden Centraal", "aantal": 3}),
    ("trein_aankomst", {"station": "Leiden Centraal", "aantal": 3}),
    ("trein_aankomst", {"station": "Utrecht", "aantal": 2, "datum": SOON.date().isoformat(), "tijd": SOON.strftime("%H:%M")}),
    ("trein_vertrek", {"station": "Utrecht", "aantal": 2, "datum": TOMORROW, "tijd": "08:00"}),
    ("trein_reis", {"van": "Den Haag Centraal", "naar": "Leiden Centraal"}),
    ("trein_storingen", {}),
    ("trein_storingen", {"station": "Leiden Centraal"}),
    ("trein_storingen", {"station": "Utrecht", "datum": NEXT_WEEK}),
    ("ov_vertrek", {"halte": "Spui", "plaats": "Den Haag", "aantal": 4}),
    ("ov_vertrek", {"halte": "Utrecht Centraal", "aantal": 4}),
    ("ov_vertrek", {"halte": "Kurhaus", "plaats": "Scheveningen", "aantal": 3}),
    ("ov_vertrek", {"halte": "Leidseplein", "plaats": "Amsterdam", "lijn": "tram 2", "aantal": 3}),
    ("ov_vertrek", {"halte": "Neude", "plaats": "Utrecht", "aantal": 3, "datum": TOMORROW, "tijd": "08:00"}),
    ("ov_vertrek", {"halte": "Den Haag Centraal", "naar": "Kurhaus", "aantal": 3, "datum": TOMORROW, "tijd": "15:00"}),
    ("ov_vertrek", {"halte": "Kort Rapenburg", "plaats": "Leiden", "naar": "Leiden Centraal", "datum": TOMORROW, "tijd": "09:30"}),
    ("ov_vertrek", {"halte": "Xyzabc", "plaats": "Leiden"}),
    ("ov_vertrek", {"halte": "Rapenburg 70", "plaats": "Leiden", "aantal": 3}),
    ("ov_vertrek", {"halte": "Paets van Troostwijkstraat", "plaats": "Den Haag", "naar": "Den Haag Centraal", "aantal": 3, "datum": TOMORROW, "tijd": "09:30"}),
    ("ov_vertrek", {"halte": "Kerkstraat 1"}),
    # Not an MCP tool: flights are only in the chat on the website.
    ("vlucht_info", {"vluchtnummer": "KL1001"}),
    ("vlucht_info", {"vluchtnummer": "KL 1001", "datum": TOMORROW}),
    ("vlucht_info", {"vluchtnummer": "KL1001", "datum": NEXT_WEEK}),
]


async def main() -> int:
    tools = await server.list_tools()
    print(f"{len(tools)} tools: {', '.join(t.name for t in tools)}")
    names = {tool.name for tool in tools}
    failed = skipped = 0
    for name, arguments in CALLS:
        # The journey planner is off until Transitous agrees (HEYJOOST_REISPLANNER), so it is skipped then.
        if name == "reis_plannen" and name not in names:
            skipped += 1
            print(f"\n== {name}: OVERGESLAGEN, staat uit (HEYJOOST_REISPLANNER)")
            continue
        print(f"\n== {name} {json.dumps(arguments, ensure_ascii=False)}")
        try:
            if name == "vlucht_info":
                data = await schiphol.vlucht_info(**arguments)
            else:
                data = (await server.call_tool(name, arguments)).structured_content
        except ToolError as exc:
            if "API-key" in str(exc):
                skipped += 1
                print(f"OVERGESLAGEN: {exc}")
            else:
                failed += 1
                print(f"FOUTMELDING: {exc}")
            continue
        print(json.dumps(data, ensure_ascii=False, indent=2)[:1500])
    print(f"\nKlaar. Foutmeldingen: {failed} (een paar zijn expres, zoals de onbekende postcode). Zonder key: {skipped}.")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    load_dotenv(find_dotenv(usecwd=True))
    sys.exit(asyncio.run(main()))
