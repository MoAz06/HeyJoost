"""Builds the list of 'vraag en antwoord' pages on rijksoverheid.nl that the regelzaken tool searches.

Rijksoverheid.nl has no search API: its own search runs in the browser. Its sitemap does list every page (CC0, and
robots.txt allows everything), so this script walks the sitemap once and keeps only the question pages. The tool
fetches a page itself when it is asked for, so only new questions need a new run, about once a month:

    uv run python scripts/rijksoverheid_vragen.py

The site blocks a visitor that asks too much at once (it did at about 12 pages a second), so this asks one page a
second and takes about twenty minutes. A run that is cut off goes on where it stopped.
"""

import json
import re
import sys
import time
from pathlib import Path

import httpx

from nederland_mcp.common import USER_AGENT

SITEMAP = "https://www.rijksoverheid.nl/sitemap.xml"
QUESTION = re.compile(
    r"<loc>https://www\.rijksoverheid\.nl/documenten/vraag-en-antwoord/([a-z0-9-]+)/([a-z0-9-]+)</loc>"
    r"(?:<lastmod>(\d{4}-\d{2}-\d{2}))?"
)
OUT = Path(__file__).parent.parent / "src" / "nederland_mcp" / "data" / "rijksoverheid_vragen.json"
PROGRESS = OUT.with_suffix(".bezig.json")
PAUSE = 1.0
BLOCKED_PAUSE = 300


def main() -> int:
    OUT.parent.mkdir(exist_ok=True)
    progress = json.loads(PROGRESS.read_text(encoding="utf-8")) if PROGRESS.exists() else {"done": [], "found": []}
    done, found = set(progress["done"]), {(topic, slug): modified for topic, slug, modified in progress["found"]}
    with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=30, follow_redirects=True) as client:
        maps = re.findall(r"<loc>([^<]+)</loc>", client.get(SITEMAP).text)
        print(f"{len(maps)} sitemaps, {len(done)} al gedaan", flush=True)
        for number, url in enumerate(maps, 1):
            if url in done:
                continue
            for attempt in range(4):
                time.sleep(PAUSE)
                response = client.get(url)
                if response.status_code not in (403, 429):
                    break
                print(f"geblokkeerd bij {url}, {BLOCKED_PAUSE} s wachten", flush=True)
                time.sleep(BLOCKED_PAUSE)
            response.raise_for_status()
            for topic, slug, modified in QUESTION.findall(response.text):
                found[(topic, slug)] = modified
            done.add(url)
            if number % 50 == 0:
                PROGRESS.write_text(json.dumps({"done": sorted(done), "found": [[*key, value] for key, value in found.items()]}), encoding="utf-8")
                print(f"{number}/{len(maps)}: {len(found)} vragen", flush=True)
    rows = sorted([topic, slug, modified] for (topic, slug), modified in found.items())
    OUT.write_text(json.dumps(rows, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    PROGRESS.unlink(missing_ok=True)
    print(f"{len(rows)} vragen in {len({row[0] for row in rows})} onderwerpen naar {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
