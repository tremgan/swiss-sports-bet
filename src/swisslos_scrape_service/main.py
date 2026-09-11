"""Scrapes football odds from Swisslos.

Swisslos has no public odds API: the page opens a WebSocket and streams
zlib-deflated JSON frames. So the scraper drives a headless Chromium, taps the
frames as they arrive, inflates them, and rebuilds the entity graph
(Competitor / Selection / Market / Event) they describe.
"""

import json
import time
import zlib
from datetime import UTC, datetime

from playwright.sync_api import sync_playwright
from pydantic import ValidationError

from core.logging_config import setup_logging
from core.models import BookmakerMatchCreate, SportsBettingOddsCreate
from core.scraper import USER_AGENT, ScrapedPair, build_session, run_forever

logger = setup_logging("swisslos_scraper")

BOOKMAKER = "Swisslos"
SWISSLOS_URL = "https://www.swisslos.ch/de/sporttip/sportwetten/fussball"

# The page streams its book progressively, so the frames are collected for a
# fixed window rather than waiting on any one signal.
FRAME_COLLECTION_SECONDS = 60
PAGE_LOAD_TIMEOUT_MS = 180_000

# Swisslos' internal URNs for the 1X2 market and its three outcomes.
MARKET_TYPE_1X2 = "asw:markettype:1"
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
    except Exception as exc:
        logger.warning(f"failed to decode payload: {exc}")
        return None


def collect_messages() -> list[dict]:
    """Drive the page and return every WebSocket frame decoded during the window."""
    messages: list[dict] = []
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
        time.sleep(FRAME_COLLECTION_SECONDS)
        page.close()
        browser.close()

    logger.info(f"collected {len(messages)} websocket messages")
    return messages


def parse_messages(messages: list[dict]) -> list[ScrapedPair]:
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
                    competitors[urn] = entity.get("name")
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

        team1 = competitors.get(competitors_refs[0]["competitor"])
        team2 = competitors.get(competitors_refs[1]["competitor"])
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

    logger.info(f"parsed {len(pairs)} matches, skipped {skipped}")
    return pairs


def scrape() -> list[ScrapedPair]:
    return parse_messages(collect_messages())


if __name__ == "__main__":
    run_forever(BOOKMAKER, scrape, session=build_session(), logger=logger)
