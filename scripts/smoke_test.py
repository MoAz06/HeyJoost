"""Live check of every tool against the real sources.

Run from the project folder: uv run python scripts/smoke_test.py
Tools that need an API-key you have not set are reported as skipped, not failed.
"""

import asyncio
import json
import sys

from dotenv import find_dotenv, load_dotenv
from mcp.server.mcpserver.exceptions import ToolError

from nederland_mcp.server import server

CALLS = [
    ("huis_info", {"postcode": "2511 cv", "huisnummer": 3}),
    ("huis_info", {"postcode": "9999ZZ", "huisnummer": 1}),
    ("schoolvakanties", {"regio": "zuid"}),
    ("schoolvakanties", {"schooljaar": "2026-2027", "regio": "Noord"}),
    ("water_actueel", {"plaats": "Hoek van Holland"}),
    ("water_actueel", {"plaats": "Den Haag", "soort": "waterstand"}),
    ("water_actueel", {"plaats": "Lobith", "soort": "waterstand"}),
    ("trein_vertrek", {"station": "Leiden Centraal", "aantal": 3}),
    ("trein_reis", {"van": "Den Haag Centraal", "naar": "Leiden Centraal"}),
    ("vlucht_info", {"vluchtnummer": "KL1001"}),
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
