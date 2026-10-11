"""School holidays from the Rijksoverheid open data API (CC0)."""

from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from ..common import cached, get_json, now_local, parse_time

URL = "https://opendata.rijksoverheid.nl/v1/infotypes/schoolholidays?output=json"
REGIONS_PAGE = "https://www.rijksoverheid.nl/themas/onderwijs/schoolvakanties/regios-schoolvakantie"
REGIONS = ("noord", "midden", "zuid")


def _clean(value: Any) -> str:
    return " ".join(str(value or "").split())


async def _all_holidays() -> list[dict]:
    data = await cached(("schoolvakanties",), 24 * 3600, lambda: get_json(URL, source="Rijksoverheid open data"))
    rows = []
    for item in data:
        for content in item.get("content", []):
            schoolyear = _clean(content.get("schoolyear"))
            for vacation in content.get("vacations", []):
                for region in vacation.get("regions", []):
                    start, end = parse_time(region.get("startdate")), parse_time(region.get("enddate"))
                    if not start or not end:
                        continue
                    rows.append({
                        "schooljaar": schoolyear,
                        "vakantie": _clean(vacation.get("type")),
                        "regio": _clean(region.get("region")),
                        "van": start.date().isoformat(),
                        "tot_en_met": end.date().isoformat(),
                    })
    return sorted(rows, key=lambda r: (r["van"], r["regio"]))


async def schoolvakanties(regio: str | None = None, schooljaar: str | None = None) -> dict:
    if regio:
        regio = regio.strip().lower()
        if regio not in REGIONS:
            raise ToolError("Regio moet 'noord', 'midden' of 'zuid' zijn.")
    rows = await _all_holidays()
    if regio:
        rows = [r for r in rows if r["regio"] in (regio, "heel Nederland")]
    if schooljaar:
        schooljaar = schooljaar.replace("/", "-").strip()
        rows = [r for r in rows if r["schooljaar"] == schooljaar]
        if not rows:
            raise ToolError(f"Geen vakanties gevonden voor schooljaar {schooljaar}.")
    else:
        today = now_local().date().isoformat()
        rows = [r for r in rows if r["tot_en_met"] >= today][:24]
    return {
        "vakanties": rows,
        "regio_per_gemeente": REGIONS_PAGE,
        "bron": "Rijksoverheid, schoolvakanties (CC0)",
    }
