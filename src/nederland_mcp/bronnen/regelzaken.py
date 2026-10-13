"""Official answers about arranging things with the government: the 'vraag en antwoord' pages of rijksoverheid.nl,
and for one gemeente its own products and services from the Samenwerkende Catalogi of KOOP. Both are CC0.

Rijksoverheid.nl has no search API, so scripts/rijksoverheid_vragen.py lists its question pages once. A question is
matched on the words in their addresses, rare words counting most, and the best pages are read when asked for:
the short answer from their own metadata and the text below it. The site blocks visitors that ask a lot at once,
so a page is kept for a day and a question reads at most three. When a page cannot be read, its question and link
still come back.
"""

import asyncio
import json
import math
import re
import unicodedata
from collections.abc import Container
from functools import cache
from pathlib import Path
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from ..common import cached, get_bytes, get_json, html_text
from . import plaats as plaatsen

INDEX = Path(__file__).parent.parent / "data" / "rijksoverheid_vragen.json"
FAQ = "https://www.rijksoverheid.nl/documenten/vraag-en-antwoord/{0}/{1}"
SRU = "https://repository.overheid.nl/sru"
BRON = "Rijksoverheid.nl en de Samenwerkende Catalogi van de overheid (CC0)"
NOTE = "Hey Joost is geen dienst van de overheid. De officiële pagina heeft altijd de laatste stand."
MAX_PAGES = 3
MAX_TEXT = 1500
MAX_PRODUCTS = 6
# What you do with a product rather than what it is: these weigh less when matching a product's name.
ACTIONS = {
    "aanvragen", "aanvraag", "verlengen", "vernieuwen", "ophalen", "doorgeven", "melden", "wijzigen", "opzeggen",
    "krijgen", "betalen", "kosten", "kost", "maken", "afspraak", "inleveren", "stopzetten", "terugvragen",
}
# Words that say little about the subject. Rare words weigh more anyway; these would only add noise.
STOP = {
    "aan", "al", "als", "ben", "bij", "dan", "dat", "de", "der", "des", "die", "dit", "doe", "doen", "een", "en", "er",
    "het", "hoe", "hoeveel", "hoelang", "ik", "in", "is", "je", "jij", "jouw", "kan", "kun", "kunt", "maar", "me",
    "mag", "mij", "mijn", "moet", "moeten", "na", "naar", "niet", "nog", "nu", "of", "om", "ook", "op", "over", "te",
    "tot", "u", "uit", "uw", "van", "voor", "waar", "wanneer", "wat", "welke", "wie", "wil", "wordt", "worden",
    "zijn", "zo", "zou", "graag", "eigenlijk", "precies", "gemeente", "regelen", "regel",
}

# Gemeenten use different words for the same thing (Leiden "trouwen", Rotterdam "huwelijk"), and the search only
# finds the exact word, so these are searched as well.
SAME = {
    "trouwen": ("huwelijk",), "huwelijk": ("trouwen",), "verhuizen": ("verhuizing",), "verhuizing": ("verhuizen",),
    "bijstand": ("bijstandsuitkering", "participatiewet"), "uitkering": ("bijstand",),
    "id": ("identiteitskaart",), "afval": ("grofvuil", "afvalpas"),
    "vuilnis": ("afval", "grofvuil"), "grofvuil": ("afval",), "parkeren": ("parkeervergunning",),
    "scheiden": ("echtscheiding",), "scheiding": ("echtscheiding",), "kappen": ("kapvergunning",),
    "boom": ("kapvergunning",), "bomen": ("kapvergunning",), "bouwen": ("omgevingsvergunning",),
    "verbouwen": ("omgevingsvergunning",), "geboorte": ("geboorteaangifte",), "baby": ("geboorteaangifte",),
    "overlijden": ("overlijdensaangifte",), "hond": ("hondenbelasting",), "honden": ("hondenbelasting",),
    "wmo": ("huishoudelijke",), "uittreksel": ("brp",), "verblijfsvergunning": ("ind",),
    "verlengen": ("aanvragen", "vernieuwen"), "vernieuwen": ("aanvragen", "verlengen"),
}


# Question words say what kind of answer is wanted (wanneer gaat mijn AOW in, hoe hoog is de kinderbijslag), so when
# matching questions they count, but only half.
ASKING = {"hoe", "hoeveel", "hoelang", "waar", "wanneer", "waarom", "wat", "welke", "wie"}
# What may follow the same stem: toeslag and toeslagen, geldig and geldigheid.
ENDINGS = {"", "e", "en", "n", "s", "es", "er", "ers", "heid", "heden", "ing", "ingen", "lijk", "lijke", "je", "jes"}


def _words(text: str, keep: set[str] | frozenset[str] = frozenset()) -> list[str]:
    plain = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    plain = re.sub(r"\bid[- ]?(kaart|bewijs)(en)?\b", "identiteitskaart", plain)
    return [word for word in re.findall(r"[a-z0-9]+", plain) if (word not in STOP or word in keep) and len(word) > 1]


def _stem(word: str) -> str:
    """A word without a plural or verb ending and with one vowel where the ending made two: aanvragen and aanvraag
    both become aanvrag."""
    for ending in ("ingen", "ing", "en", "e", "s", "n"):
        if word.endswith(ending) and len(word) - len(ending) >= 4:
            word = word[: -len(ending)]
            break
    return re.sub(r"([aeou])\1(?=[^aeiou]$)", r"\1", word)


def _match(asked: str, word: str, known: Container[str] = ()) -> float:
    """How well a word from the question matches a word of a page address: the same, the same stem (toeslagen and
    toeslag, aanvraag and aanvragen, geldig and geldigheid) or a compound of it and another known word (huurtoeslag
    and toeslag). Krijg and krijgsmacht, or zorgt and zorgtoeslag, do not match."""
    if asked == word:
        return 1.0
    short, long = sorted((_stem(asked), _stem(word)), key=len)
    if len(short) >= 4 and long.startswith(short) and long[len(short):] in ENDINGS:
        return 0.8
    short, long = sorted((asked, word), key=len)
    rest = long[len(short):] if long.startswith(short) else long[: -len(short)] if long.endswith(short) else ""
    if len(short) >= 4 and len(rest) >= 4 and rest in known:
        return 0.5
    return 0.0


class _Index:
    """The question pages of rijksoverheid.nl, searchable by the words in their addresses."""

    def __init__(self, rows: list[list[str]]) -> None:
        self.pages = [(topic, slug, modified) for topic, slug, modified in rows]
        self.words: list[dict[str, float]] = []
        seen: dict[str, int] = {}
        for topic, slug, _ in self.pages:
            # The question itself counts fully, the topic it is filed under a little less.
            # A question filed under a topic with the same word (paspoort) is more about it than one that only
            # mentions it (where is the bsn on my passport, under privacy).
            weights = {word: 0.6 for word in _words(topic.replace("-", " "))}
            weights.update({
                word: 0.5 if word in ASKING else 1.2 if word in weights else 1.0
                for word in _words(slug.replace("-", " "), ASKING)
            })
            self.words.append(weights)
            for word in weights:
                seen[word] = seen.get(word, 0) + 1
        total = len(self.pages) + 1
        self.rarity = {word: math.log(total / (count + 1)) + 0.1 for word, count in seen.items()}
        self.with_word: dict[str, list[int]] = {}
        for number, weights in enumerate(self.words):
            for word in weights:
                self.with_word.setdefault(word, []).append(number)

    def rare(self, word: str) -> float:
        return self.rarity.get(word, math.log(len(self.pages) + 1) + 0.1)

    def search(self, words: list[str]) -> list[tuple[float, tuple[str, str, str]]]:
        scores: dict[int, dict[str, float]] = {}
        for asked in dict.fromkeys(words):
            # What you want to do (aanvragen, verlengen) says less than what it is about, and a word that means
            # about the same (a passport you renew is one you apply for) less than the word itself.
            weight = 0.5 if asked in ACTIONS else 1.0
            for alike, factor in ((asked, 1.0), *((same, 0.5) for same in SAME.get(asked, ()))):
                for word in self.rarity:
                    strength = _match(alike, word, self.rarity)
                    if not strength:
                        continue
                    # A rare word that only looks like a common one in the question (langer, lang) counts as common.
                    rare = self.rarity[word] if strength == 1.0 else min(self.rarity[word], self.rare(alike))
                    for number in self.with_word[word]:
                        value = weight * factor * strength * rare * self.words[number][word]
                        best = scores.setdefault(number, {})
                        best[asked] = max(best.get(asked, 0.0), value)
        ranked = sorted(((sum(parts.values()), number) for number, parts in scores.items()), reverse=True)
        return [(score, self.pages[number]) for score, number in ranked]


@cache
def _index() -> _Index:
    try:
        return _Index(json.loads(INDEX.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return _Index([])


def _from_address(slug: str) -> str:
    """The question as its address spells it, for when the page itself cannot be read."""
    text = slug.replace("-", " ")
    return text[:1].upper() + text[1:] + "?"


def _without(text: str, start: str) -> str:
    """The text without the short answer that the page repeats at the top, compared without spaces, as the page
    breaks it over lines and glues a link to it. The short answer has its links in brackets, the page does not."""
    want = re.sub(r"\s", "", re.sub(r"\[https?://[^\]\s]*\]", "", start))
    if not want or not re.sub(r"\s", "", text).startswith(want):
        return text
    seen = 0
    for position, char in enumerate(text):
        if not char.isspace():
            seen += 1
            if seen == len(want):
                return text[position + 1:].lstrip()
    return ""


def _page(raw: bytes, url: str) -> dict[str, Any]:
    page = raw.decode("utf-8", "replace")
    question = answer = None
    for block in re.findall(r'<script type="application/ld\+json">(.*?)</script>', page, re.S):
        try:
            graph = json.loads(block).get("@graph") or []
        except ValueError:
            continue
        for item in graph:
            if item.get("@type") == "FAQPage":
                entity = (item.get("mainEntity") or [{}])[0]
                question = entity.get("name")
                answer = (entity.get("acceptedAnswer") or {}).get("text")
    modified = re.search(r'<meta name="DCTERMS\.modified" content="(\d{4}-\d{2}-\d{2})', page)
    main = page[page.find("<main"):]
    # The feedback bar below the answer starts inside a tag, so the cut is at the start of that tag.
    end = main.find("__cq-feedback-bar")
    main = main[: main.rfind("<", 0, end)] if end > 0 else main[: main.find("</main>")]
    links = []
    for href, label in re.findall(r'<a [^>]*href="([^"#][^"]*)"[^>]*>(.*?)</a>', main, re.S):
        label = html_text(label)
        if href.startswith("/"):
            href = "https://www.rijksoverheid.nl" + href
        if href.startswith("http") and label and href != url and all(href != link["link"] for link in links):
            links.append({"tekst": label, "link": href})
    lines = html_text(main).splitlines()
    more = _without("\n".join(lines), answer or "")
    row: dict[str, Any] = {"vraag": question, "antwoord": answer}
    if more:
        row["meer"] = more[:MAX_TEXT] + (" …" if len(more) > MAX_TEXT else "")
    if links:
        row["links"] = links[:6]
    if modified:
        row["bijgewerkt"] = modified[1]
    return row


async def _faq(topic: str, slug: str) -> dict[str, Any]:
    url = FAQ.format(topic, slug)
    try:
        raw = await cached(("rijksoverheid-vraag", topic, slug), 24 * 3600, lambda: get_bytes(url, source="Rijksoverheid.nl"))
        row = _page(raw, url)
    except ToolError:
        row = {}
    if not row.get("vraag"):
        row = {"vraag": _from_address(slug), "antwoord": "Deze pagina kon ik nu niet lezen. De link werkt wel."}
    return {**row, "link": url}


def _value(field: Any) -> Any:
    """SRU fields come as a string, as {'$': value} or as a list of those."""
    if isinstance(field, list):
        return [_value(item) for item in field]
    return field.get("$") if isinstance(field, dict) else field


async def _sru(query: str) -> list[dict]:
    params = {"query": query, "maximumRecords": 50, "httpAccept": "application/json"}
    data = await cached(("sc", query), 24 * 3600, lambda: get_json(SRU, params=params, source="Samenwerkende Catalogi"))
    records = ((data.get("searchRetrieveResponse") or {}).get("records") or {}).get("record") or []
    return records if isinstance(records, list) else [records]


async def _products(gemeente: str, words: list[str]) -> list[dict[str, Any]]:
    """Products of one gemeente for the most telling words of the question. The search reads whole pages, so a page
    about a driving licence that mentions a passport is found too; the name of the product decides the order."""
    base = f'c.product-area==samenwerkendecatalogi AND dt.creator=="{gemeente}"'
    # What it is about (paspoort) before what you want to do with it (verlengen), which finds every product.
    words = sorted(words, key=lambda word: word in ACTIONS)
    groups = [words[:3], words[:1]] if len(words) > 1 else [words]

    def term(word: str) -> str:
        return "(" + " OR ".join(f'cql.textAndIndexes="{same}"' for same in (word, *SAME.get(word, ()))) + ")"

    found = await asyncio.gather(*(_sru(base + "".join(f" AND {term(word)}" for word in group)) for group in groups))
    rows, seen, known = [], set(), _index().rarity
    for record in (record for records in found for record in records):
        meta = (((record.get("recordData") or {}).get("gzd") or {}).get("originalData") or {}).get("meta") or {}
        core, extra, more = meta.get("owmskern") or {}, meta.get("owmsmantel") or {}, meta.get("tpmeta") or {}
        link = more.get("bronIdentifier")
        if more.get("organisatietype") != "gemeente" or not link or link in seen:
            continue
        seen.add(link)
        audience = _value(extra.get("audience"))
        audience = audience if isinstance(audience, list) else [audience]
        title = _value(core.get("title")) or ""
        # The standard name of the product (paspoort, parkeervergunning) says best what it is about.
        product = _words(str(_value(more.get("uniformeProductnaam")) or ""))
        named = _words(title)
        fit = sum(
            (0.3 if word in ACTIONS else 1.0) * max(
                [2 * _match(same, own, known) for same in (word, *SAME.get(word, ())) for own in product]
                + [_match(same, own, known) for same in (word, *SAME.get(word, ())) for own in named] + [0.0]
            )
            for word in words
        )
        rows.append((fit, "particulier" in audience, -len(title), {
            "naam": title,
            "samenvatting": " ".join((_value(extra.get("abstract")) or "").split())[:300],
            "online_aanvragen": more.get("onlineAanvragen") == "ja",
            "voor": " en ".join(sorted(str(item) for item in audience if item)) or None,
            "link": link,
        }))
    best = max((row[0] for row in rows), default=0.0)
    rows = sorted((row for row in rows if row[0] >= max(0.5, best / 2)), key=lambda row: row[:3], reverse=True)
    return [{key: value for key, value in row[3].items() if value is not None} for row in rows[:MAX_PRODUCTS]]


async def regelzaken(vraag: str, gemeente: str | None = None) -> dict[str, Any]:
    vraag = " ".join(vraag.split())
    town = None
    if gemeente and gemeente.strip():
        try:
            town = await plaatsen.zoek(gemeente)
        except ToolError as exc:
            raise ToolError(f"Geen gemeente gevonden die lijkt op '{gemeente}'. Noem de gemeente of de plaats.") from exc
    # The name of the place says nothing about the subject, and would find every product of that gemeente.
    place = set(_words(f"{gemeente or ''} {town.naam} {town.gemeente}")) if town else set()
    index = _index()
    asked = [word for word in dict.fromkeys(_words(vraag, ASKING)) if word not in place]
    words = sorted((word for word in asked if word not in ASKING), key=index.rare, reverse=True)
    if not words:
        raise ToolError("Waar gaat je vraag over? Noem het onderwerp, bijvoorbeeld paspoort, huurtoeslag of AOW.")

    found = index.search(asked)
    best = found[0][0] if found else 0.0
    # Pages that score far below the best one are about something else.
    pages = [page for score, page in found[:MAX_PAGES] if score >= max(best * 0.6, 1.5)]
    result: dict[str, Any] = {}
    if pages:
        result["rijksoverheid"] = list(await asyncio.gather(*(_faq(topic, slug) for topic, slug, _ in pages)))

    if town:
        products = await _products(town.gemeente, words)
        name = plaatsen.common_name(town.gemeente)
        result["gemeente"] = {"naam": name, "producten": products} if products else {
            "naam": name, "producten": [], "uitleg": f"Ik vond hierover niets op de site van de gemeente {name}.",
        }

    if not result:
        raise ToolError(
            f"Ik vond geen officieel antwoord op '{vraag}'. Probeer andere woorden. Gaat het om iets bij je gemeente, "
            "noem dan ook de gemeente."
        )
    result["let_op"] = NOTE
    result["bron"] = BRON
    return result
