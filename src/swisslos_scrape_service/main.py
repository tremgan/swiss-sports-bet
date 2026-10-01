"""Scrapes football odds from Swisslos.

Swisslos has no public odds API: the page opens a WebSocket and streams
zlib-deflated JSON frames. So the scraper drives a headless Chromium, taps the
frames as they arrive, inflates them, and rebuilds the entity graph
(Competitor / Selection / Market / Event) they describe.

The link to a fixture's own page comes from the DOM rather than the frames: an
Event carries a competition and a round, but the URL also needs a `?t=` round
id that is nowhere on the wire. The anchors are already rendered beside the
odds, so the browser the scraper is driving anyway is the cheapest place to
read them.
"""

import json
import re
import time
import unicodedata
import zlib
from datetime import UTC, datetime
from urllib.parse import urljoin

from core.logging_config import setup_logging
from core.models import BookmakerMatchCreate, SportsBettingOddsCreate
from core.scraper import USER_AGENT, ScrapedPair, build_session, run_from_cli
from playwright.sync_api import sync_playwright
from pydantic import ValidationError

logger = setup_logging("swisslos_scraper")

BOOKMAKER = "Swisslos"
SWISSLOS_URL = "https://www.swisslos.ch/de/sporttip/sportwetten/fussball"
SWISSLOS_BASE_URL = "https://www.swisslos.ch"

# The page streams its book progressively, so the frames are collected for a
# fixed window rather than waiting on any one signal. The window is spent
# scrolling rather than sleeping, because the list renders as it is reached and
# an anchor that never rendered cannot be read.
FRAME_COLLECTION_SECONDS = 60
SCROLL_STEP_PX = 4000
SCROLL_INTERVAL_MS = 1500
PAGE_LOAD_TIMEOUT_MS = 180_000

# .../fussball/<competition>[/<round>]/<home>-vs-<away>[?t=<round id>]
FIXTURE_HREF = re.compile(
    r"/de/sporttip/sportwetten/fussball/.+?/([a-z0-9-]+-vs-[a-z0-9-]+)(?:\?|$)"
)

# Umlauts reach the URL transliterated, not stripped: "Türkei" is "tuerkei" and
# "Färöer" is "faeroeer", so folding them to ASCII first would miss both.
UMLAUTS = str.maketrans(
    {"ä": "ae", "ö": "oe", "ü": "ue", "Ä": "ae", "Ö": "oe", "Ü": "ue", "ß": "ss"}
)

# Swisslos' internal URNs for the 1X2 market and its three outcomes.
MARKET_TYPE_1X2 = "asw:markettype:1"
# Competitor names are localised on the wire; this is the locale Loro is
# scraped in, so the two feeds name a team the same way.
COMPETITOR_LOCALE = "de"

SELECTION_TYPE_MAP = {
    "asw:selectiontype:1": "home",
    "asw:selectiontype:2": "draw",
    "asw:selectiontype:3": "away",
}


def decode_binary_payload(payload: bytes) -> dict | None:
    """Inflate one raw WebSocket frame. Frames are raw deflate, hence wbits=-15."""
    try:
        decompressed = zlib.decompress(payload, wbits=-15)
        return json.loads(decompressed.decode("utf-8"))
    except (zlib.error, UnicodeDecodeError, json.JSONDecodeError) as exc:
        logger.warning(f"failed to decode payload: {exc}")
        return None


def _slug(name: str) -> str:
    """Fold a team name the way Swisslos folds it into a URL."""
    folded = unicodedata.normalize("NFKD", name.translate(UMLAUTS).lower())
    ascii_only = folded.encode("ascii", "ignore").decode()
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", ascii_only)).strip("-")


def fixture_slug(team1: str, team2: str) -> str:
    """The `<home>-vs-<away>` segment identifying a fixture in its URL."""
    return f"{_slug(team1)}-vs-{_slug(team2)}"


def harvest_links(page) -> dict[str, str]:
    """Map each rendered fixture's slug to its absolute URL.

    Keyed on the slug because the href carries no event id, so the fixture's
    own team names are the only thing both sides share. A name the page
    shortens — "Real Sociedad San Sebastian B" is "u21-real-sociedad" there —
    simply does not match, and that fixture goes unlinked rather than linked
    to a neighbour.
    """
    links: dict[str, str] = {}
    hrefs = page.eval_on_selector_all(
        "a[href]", "els => els.map(e => e.getAttribute('href'))"
    )
    for href in hrefs:
        found = FIXTURE_HREF.search(href) if href else None
        if found:
            links[found.group(1)] = urljoin(SWISSLOS_BASE_URL, href)
    return links


def collect_messages() -> tuple[list[dict], dict[str, str]]:
    """Drive the page, returning its decoded frames and its fixture links."""
    messages: list[dict] = []
    links: dict[str, str] = {}
    logger.info("launching headless browser...")

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(user_agent=USER_AGENT)
        page = context.new_page()

        def on_websocket(ws):
            logger.info(f"websocket opened: {ws.url}")

            def on_frame(payload: bytes):
                message = decode_binary_payload(payload)
                if message:
                    messages.append(message)

            ws.on("framereceived", on_frame)
            ws.on("close", lambda _: logger.info("websocket closed"))

        page.on("websocket", on_websocket)
        logger.info("navigating to Swisslos football page...")
        page.goto(
            SWISSLOS_URL,
            timeout=PAGE_LOAD_TIMEOUT_MS,
            wait_until="domcontentloaded",
        )
        logger.info(
            f"page loaded, collecting frames for {FRAME_COLLECTION_SECONDS}s..."
        )
        deadline = time.monotonic() + FRAME_COLLECTION_SECONDS
        while time.monotonic() < deadline:
            page.mouse.wheel(0, SCROLL_STEP_PX)
            page.wait_for_timeout(SCROLL_INTERVAL_MS)
            # Harvested every pass, not once at the end: the list virtualises,
            # so an anchor scrolled past may no longer be in the DOM.
            links.update(harvest_links(page))
        page.close()
        browser.close()

    logger.info(f"collected {len(messages)} websocket messages, {len(links)} links")
    return messages, links


def parse_messages(messages: list[dict], links: dict[str, str]) -> list[ScrapedPair]:
    """Rebuild the entity graph from the frames and emit (match, odds) pairs."""
    competitors: dict[str, str] = {}
    selections: dict[str, dict] = {}
    markets: dict[str, dict] = {}
    events: list[dict] = []

    for msg in messages:
        payload = msg.get("payload", [])
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError as exc:
                logger.warning(f"skipping frame with unparseable payload: {exc}")
                continue

        for item in payload:
            if not isinstance(item, dict):
                continue
            body = item.get("body", {})
            if not isinstance(body, dict):
                continue
            entities = body.get("snapshotUpdate", {}).get("snapshotUpdateItems", [])
            for e in entities:
                if not isinstance(e, dict):
                    continue
                entity_type = e.get("type")
                entity = e.get("entity", {})
                urn = entity.get("urn")

                if entity_type == "Competitor":
                    # `name` is English, which only matches Loro for the names
                    # that happen to be spelt the same in both languages:
                    # "Germany" never meets Loro's de-CH "Deutschland", so an
                    # international break links almost nothing. The entity
                    # carries the German form outright. Clubs usually have no
                    # `de` entry, and there `name` is already the German form.
                    translations = entity.get("translations") or {}
                    competitors[urn] = translations.get(
                        COMPETITOR_LOCALE
                    ) or entity.get("name")
                elif entity_type == "Selection":
                    selections[urn] = {
                        "type": entity.get("type"),
                        "odds": entity.get("odds"),
                    }
                elif entity_type == "Market":
                    markets[urn] = {
                        "type": entity.get("type"),
                        "selections": entity.get("selections", []),
                    }
                elif entity_type == "Event":
                    events.append(entity)

    logger.info(
        f"{len(competitors)=} {len(selections)=} {len(markets)=} {len(events)=}"
    )

    pairs: list[ScrapedPair] = []
    skipped = 0

    for event in events:
        competitors_refs = event.get("eventCompetitors", [])
        if len(competitors_refs) != 2:
            skipped += 1
            continue

        # Swisslos does not tag the sides: home is the first competitor by
        # convention, where Loro states HOME/AWAY outright. The odds below are
        # role-tagged, so a reversed feed would pin them to the wrong name.
        try:
            team1 = competitors.get(competitors_refs[0]["competitor"])
            team2 = competitors.get(competitors_refs[1]["competitor"])
        except (KeyError, TypeError):
            logger.warning(f"malformed competitors for event {event.get('urn')!r}")
            skipped += 1
            continue
        if not team1 or not team2:
            logger.warning(f"missing competitor name for event {event.get('urn')!r}")
            skipped += 1
            continue

        match_label = f"{team1} vs {team2}"
        match_datetime = datetime.fromisoformat(
            event["startTime"].replace("Z", "+00:00")
        )
        match_datetime = match_datetime.astimezone(UTC).replace(tzinfo=None)

        market_1x2 = None
        for market_urn in event.get("markets", []):
            market = markets.get(market_urn)
            if market and market["type"] == MARKET_TYPE_1X2:
                market_1x2 = market
                break

        if not market_1x2:
            logger.warning(f"no 1X2 market found for {match_label!r}")
            skipped += 1
            continue

        odds_by_type = {}
        for sel_urn in market_1x2["selections"]:
            sel = selections.get(sel_urn)
            if sel:
                role = SELECTION_TYPE_MAP.get(sel["type"])
                if role:
                    odds_by_type[role] = sel["odds"]

        if "home" not in odds_by_type or "away" not in odds_by_type:
            logger.warning(f"incomplete odds for {match_label!r}: {odds_by_type}")
            skipped += 1
            continue

        try:
            pairs.append(
                (
                    BookmakerMatchCreate(
                        bookmaker=BOOKMAKER,
                        match_label=match_label,
                        match_datetime=match_datetime,
                        team1=team1,
                        team2=team2,
                        url=links.get(fixture_slug(team1, team2)),
                    ),
                    SportsBettingOddsCreate(
                        team1_odds=odds_by_type["home"],
                        team2_odds=odds_by_type["away"],
                        draw_odds=odds_by_type.get("draw"),
                    ),
                )
            )
        except ValidationError as exc:
            logger.warning(f"invalid odds for {match_label!r}, skipping: {exc}")
            skipped += 1
            continue

    linked = sum(1 for match, _ in pairs if match.url)
    logger.info(f"parsed {len(pairs)} matches ({linked} linked), skipped {skipped}")
    return pairs


def scrape() -> list[ScrapedPair]:
    return parse_messages(*collect_messages())


if __name__ == "__main__":
    run_from_cli(BOOKMAKER, scrape, session=build_session(), logger=logger)
