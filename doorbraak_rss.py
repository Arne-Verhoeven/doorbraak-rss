#!/usr/bin/env python3
"""
Maakt een RSS-feed (feed.xml) van doorbraak.be door de overzichtspagina's te scrapen.

Installatie:
    pip install requests beautifulsoup4 feedgen

Gebruik:
    python doorbraak_rss.py                      # schrijft feed.xml
    python doorbraak_rss.py --enrich             # haalt per artikel ook samenvatting + datum op
    python doorbraak_rss.py -o /pad/naar/feed.xml
    python doorbraak_rss.py --url https://doorbraak.be/blok/column   # extra/andere pagina's

Let op: betaalde artikels tonen enkel hun intro. Wees beleefd: draai dit
hooguit enkele keren per uur (bv. via cron).
"""
import argparse
import re
import sys
import time
from datetime import datetime
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from feedgen.feed import FeedGenerator

BASE = "https://doorbraak.be"
DEFAULT_PAGES = [f"{BASE}/", f"{BASE}/focus"]
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; personal-rss-script/1.0)",
    "Accept-Language": "nl-BE,nl;q=0.9",
}
TZ = ZoneInfo("Europe/Brussels")

# Eerste padsegmenten die nooit een artikel zijn
EXCLUDE_FIRST = {
    "blok", "auteurs", "onderwerp", "nuxt", "wp-content", "cdn-cgi", "account",
}
# Losse pagina's (1 segment) die geen artikel zijn
EXCLUDE_SINGLE = {
    "magazine", "reizen", "focus", "meest-gelezen", "archief", "adverteren",
    "steun", "investeren", "contact", "algemene-voorwaarden", "wat-is-doorbraak",
    "cartoon", "cartoons", "recensies", "boekennieuws", "doorbraaknieuws",
}
# Secties waarvan /sectie/slug een artikel is
SECTIONS = {"cartoons", "recensies", "boekennieuws", "doorbraaknieuws"}

DATE_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b")


def is_article(url: str) -> bool:
    p = urlparse(url)
    if p.netloc.replace("www.", "") != "doorbraak.be":
        return False
    parts = [s for s in p.path.split("/") if s]
    if not parts or parts[0] in EXCLUDE_FIRST:
        return False
    if len(parts) == 1:
        return parts[0] not in EXCLUDE_SINGLE
    return parts[0] in SECTIONS and len(parts) == 2


def clean_url(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}{p.path}".rstrip("/")


def parse_date(text: str):
    m = DATE_RE.search(text or "")
    if not m:
        return None
    d, mth, y = map(int, m.groups())
    try:
        return datetime(y, mth, d, 12, 0, tzinfo=TZ)
    except ValueError:
        return None


def parse_listing(html: str, page_url: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")

    # Groepeer alle <a>-tags per artikel-URL
    by_url: dict[str, list] = {}
    for a in soup.find_all("a", href=True):
        url = clean_url(urljoin(page_url, a["href"]))
        if is_article(url):
            by_url.setdefault(url, []).append(a)

    def article_hrefs_in(node) -> set:
        found = set()
        for a in node.find_all("a", href=True):
            u = clean_url(urljoin(page_url, a["href"]))
            if is_article(u):
                found.add(u)
        return found

    items = []
    for url, anchors in by_url.items():
        # De titel is de langste ankertekst (de afbeeldingslink heeft geen tekst)
        title_a = max(anchors, key=lambda a: len(a.get_text(strip=True)))
        title = title_a.get_text(" ", strip=True)
        if len(title) < 8:
            continue

        # Klim omhoog tot de container net meer dan 1 artikel bevat
        container = title_a
        while container.parent and container.parent.name not in ("body", "html", "[document]"):
            if len(article_hrefs_in(container.parent)) > 1:
                break
            container = container.parent

        text = container.get_text(" ", strip=True)
        author_a = container.find("a", href=re.compile(r"/auteurs/"))
        author = author_a.get_text(strip=True) if author_a else None
        img = container.find("img")
        image = None
        if img:
            image = img.get("src") or img.get("data-src")
            if image:
                image = urljoin(page_url, image)

        # Beschrijving: langste losse tekststuk dat niet titel/auteur/datum is
        skip = {title, author or ""}
        pieces = [
            s.strip() for s in container.stripped_strings
            if s.strip() not in skip and not DATE_RE.fullmatch(s.strip())
        ]
        pieces = [s for s in pieces if len(s) > 40]
        description = max(pieces, key=len) if pieces else ""

        items.append({
            "url": url,
            "title": title,
            "author": author,
            "image": image,
            "description": description,
            "date": parse_date(text),
        })
    return items


def enrich(item: dict, session: requests.Session) -> None:
    """Haalt og:description en publicatiedatum uit het artikel zelf."""
    try:
        r = session.get(item["url"], headers=HEADERS, timeout=20)
        r.raise_for_status()
    except requests.RequestException as e:
        print(f"  ! kon {item['url']} niet ophalen: {e}", file=sys.stderr)
        return
    soup = BeautifulSoup(r.text, "html.parser")

    def meta(*names):
        for n in names:
            tag = soup.find("meta", attrs={"property": n}) or soup.find("meta", attrs={"name": n})
            if tag and tag.get("content"):
                return tag["content"].strip()
        return None

    desc = meta("og:description", "description")
    if desc:
        item["description"] = desc
    published = meta("article:published_time")
    if published:
        try:
            item["date"] = datetime.fromisoformat(published.replace("Z", "+00:00"))
        except ValueError:
            pass
    if not item.get("image"):
        item["image"] = meta("og:image")


def build_feed(items: list[dict], output: str) -> None:
    fg = FeedGenerator()
    fg.title("Doorbraak.be (onofficiële feed)")
    fg.link(href=BASE, rel="alternate")
    fg.description("Onofficiële RSS-feed van Doorbraak.be, gegenereerd via scraping.")
    fg.language("nl-BE")

    # Oudste eerst toevoegen: feedgen zet de laatst toegevoegde bovenaan
    dated = sorted(items, key=lambda i: i["date"] or datetime(1970, 1, 1, tzinfo=TZ))
    for it in dated:
        fe = fg.add_entry()
        fe.id(it["url"])
        fe.title(it["title"])
        fe.link(href=it["url"])
        if it["author"]:
            fe.author({"name": it["author"]})
        desc = it["description"] or ""
        if it.get("image"):
            desc = f'<p><img src="{it["image"]}" alt=""/></p><p>{desc}</p>'
        fe.description(desc or it["title"])
        if it["date"]:
            fe.pubDate(it["date"])
    fg.rss_file(output, pretty=True)


def main() -> None:
    ap = argparse.ArgumentParser(description="Maak een RSS-feed van doorbraak.be")
    ap.add_argument("--url", action="append", help="pagina om te scrapen (herhaalbaar)")
    ap.add_argument("-o", "--output", default="feed.xml")
    ap.add_argument("--limit", type=int, default=40, help="max aantal artikels")
    ap.add_argument("--enrich", action="store_true",
                    help="haal per artikel samenvatting en exacte datum op (trager)")
    args = ap.parse_args()

    pages = args.url or DEFAULT_PAGES
    session = requests.Session()
    seen: dict[str, dict] = {}

    for page in pages:
        print(f"Ophalen: {page}")
        try:
            r = session.get(page, headers=HEADERS, timeout=20)
            r.raise_for_status()
        except requests.RequestException as e:
            print(f"  ! mislukt: {e}", file=sys.stderr)
            continue
        for it in parse_listing(r.text, page):
            seen.setdefault(it["url"], it)
        time.sleep(1)

    items = list(seen.values())[: args.limit]
    if not items:
        sys.exit("Geen artikels gevonden. Is de HTML-structuur veranderd, of wordt je geblokkeerd?")

    if args.enrich:
        for i, it in enumerate(items, 1):
            print(f"Verrijken {i}/{len(items)}: {it['title'][:60]}")
            enrich(it, session)
            time.sleep(1)

    build_feed(items, args.output)
    print(f"Klaar: {len(items)} artikels geschreven naar {args.output}")


if __name__ == "__main__":
    main()
