"""Builds the table of road locations that the verkeer tool uses to name a place on the road, like "A12 bij afrit 6
Zoetermeer, richting Utrecht".

NDW's traffic picture marks a jam or closure with a code from VILD, the Dutch location table for traffic messages
(open data from NDW, CC0). This script reads the table from NDW's zip and keeps what the tool needs:

    uv run python scripts/ndw_vild.py [VILD6.13.A.zip]

Without a file it downloads the zip (about 40 MB) once. Run it again when NDW's feed moves to a new version of the
table: the tool says so in the log ("VILD-versie").
"""

import io
import json
import struct
import sys
import zipfile
from pathlib import Path

import httpx

from nederland_mcp.common import USER_AGENT

VERSION = "6.13.A"
URL = f"https://opendata.ndw.nu/VILD{VERSION}.zip"
OUT = Path(__file__).parent.parent / "src" / "nederland_mcp" / "data" / "vild.json"


def rows(data: bytes) -> list[dict[str, str]]:
    """The records of a dBase table."""
    count, header, size = struct.unpack("<IHH", data[4:12])
    fields, position = [], 32
    while data[position] != 0x0D:
        fields.append((data[position:position + 11].split(b"\0")[0].decode("ascii"), data[position + 16]))
        position += 32
    found = []
    for number in range(count):
        record = data[header + number * size:header + (number + 1) * size]
        if record[:1] == b"*":
            continue
        offset, row = 1, {}
        for name, length in fields:
            row[name] = record[offset:offset + length].decode("cp1252", "replace").strip()
            offset += length
        found.append(row)
    return found


def main() -> int:
    if len(sys.argv) > 1:
        archive = Path(sys.argv[1]).read_bytes()
    else:
        archive = httpx.get(URL, headers={"User-Agent": USER_AGENT}, timeout=300, follow_redirects=True).content
    with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
        name = next(name for name in zipped.namelist() if name.endswith(f"VILD{VERSION}.dbf"))
        table = rows(zipped.read(name))
    by_number = {row["LOC_NR"]: row for row in table}
    points = {}
    for row in table:
        if not row["LOC_TYPE"].startswith("P"):
            continue
        line = by_number.get(row["LIN_REF"]) or {}
        area = by_number.get(row["AREA_REF"]) or {}
        town = area.get("FIRST_NAME") if area.get("LOC_TYPE", "").startswith(("A8", "A9")) else ""
        # Road, street, kind of place, its names, exit number, town, and where the road goes either way.
        points[row["LOC_NR"]] = [
            row["ROADNUMBER"], row["ROADNAME"], row["LOC_DES"], row["FIRST_NAME"], row["SECND_NAME"], row["EXIT_NR"],
            town, line.get("FIRST_NAME", ""), line.get("SECND_NAME", ""),
        ]
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(
        json.dumps({"versie": VERSION, "punten": points}, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8"
    )
    print(f"{len(points)} punten uit VILD {VERSION} naar {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
