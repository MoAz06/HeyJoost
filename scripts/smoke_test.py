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

from nederland_mcp.common import now_local
from nederland_mcp.server import server

SOON = now_local() + timedelta(minutes=30)
TOMORROW = (now_local() + timedelta(days=1)).date().isoformat()

CALLS = [
    ("huis_info", {"postcode": "2511 cv", "huisnummer": 3}),
    ("huis_info", {"straat": "Rapenburg", "huisnummer": 70, "plaats": "Leiden"}),
    ("huis_info", {"straat": "Spui", "huisnummer": 70, "plaats": "Den Haag"}),
    ("huis_info", {"straat": "Rapenburg", "huisnummer": 70}),
    ("huis_info", {"postcode": "9999ZZ", "huisnummer": 1}),
    ("schoolvakanties", {"regio": "zuid"}),
    ("schoolvakanties", {"schooljaar": "2026-2027", "regio": "Noord"}),
    ("water_actueel", {"plaats": "Hoek van Holland"}),
    ("water_actueel", {"plaats": "Den Haag", "soort": "waterstand"}),
    ("water_actueel", {"plaats": "Scheveningen", "soort": "watertemperatuur"}),
    ("water_actueel", {"plaats": "Lobith", "soort": "waterstand"}),
    ("trein_vertrek", {"station": "Leiden Centraal", "aantal": 3}),
    ("trein_aankomst", {"station": "Leiden Centraal", "aantal": 3}),
    ("trein_aankomst", {"station": "Utrecht", "aantal": 2, "datum": SOON.date().isoformat(), "tijd": SOON.strftime("%H:%M")}),
    ("trein_vertrek", {"station": "Utrecht", "aantal": 2, "datum": TOMORROW, "tijd": "08:00"}),
    ("trein_reis", {"van": "Den Haag Centraal", "naar": "Leiden Centraal"}),
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
]


async def main() -> int:
    tools = await server.list_tools()
    print(f"{len(tools)} tools: {', '.join(t.name for t in tools)}")
    failed = skipped = 0
    for name, arguments in CALLS:
        print(f"\n== {name} {json.dumps(arguments, ensure_ascii=False)}")
        try:
            result = await server.call_tool(name, arguments)
        except ToolError as exc:
            if "API-key" in str(exc):
                skipped += 1
                print(f"OVERGESLAGEN: {exc}")
            else:
                failed += 1
                print(f"FOUTMELDING: {exc}")
            continue
        print(json.dumps(result.structured_content, ensure_ascii=False, indent=2)[:1500])
    print(f"\nKlaar. Foutmeldingen: {failed} (een paar zijn expres, zoals de onbekende postcode). Zonder key: {skipped}.")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    load_dotenv(find_dotenv(usecwd=True))
    sys.exit(asyncio.run(main()))
