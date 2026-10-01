"""Scrapes 1X2 football odds from Loro's sportsbook API.

Loro runs on OpenBet, whose content-service returns fixtures and markets from a
single `event-list` call. Two query parameters are load-bearing and easy to lose:
without `includeChildMarkets` the response carries no markets at all, and
without `maxMarkets` it carries every market for every event — roughly 120MB per
run instead of 1.5MB. There is no server-side market filter, so
`prioritisePrimaryMarkets` is what makes the single returned market the 1X2.
"""

import time
from datetime import UTC, datetime, timedelta
from typing import Any

import requests
from core.logging_config import setup_logging
from core.models import BookmakerMatchCreate, SportsBettingOddsCreate
from core.scraper import ScrapedPair, build_session, run_from_cli
from pydantic import ValidationError

logger = setup_logging("loro_scraper")

BOOKMAKER = "Loro"
LORO_API_URL = (
    "https://content.sportbetting.jeux.loro.ch/content-service/api/v1/q/event-list"
)

# From the drilldown tree: node 11, "Football", code "soccer".
FOOTBALL_DRILLDOWN_TAG = "11"
MATCH_RESULT_GROUP = "MATCH_RESULT"
SCRAPE_WINDOW_DAYS = 3

# The backend intermittently answers 200 with a DataFetchingException body,
# independent of the parameters sent. Observed failing and succeeding for the
# same request minutes apart, so a couple of retries clears it.
FETCH_ATTEMPTS = 3
FETCH_RETRY_BACKOFF_SECONDS = 2.0

# German team names line up with Swisslos ("Thun", not "Thoune"), which is what
# the cross-bookmaker matcher has to reconcile.
LOCALE = "de-CH"

# Outcome subType codes. Language-independent, unlike the outcome name, which
# is the localised team name (or "X" for the draw).
HOME, DRAW, AWAY = "H", "D", "A"
OUTCOME_SIDES = frozenset({HOME, DRAW, AWAY})


def _request_params() -> dict[str, str]:
    now = datetime.now(UTC)
    window_end = now + timedelta(days=SCRAPE_WINDOW_DAYS)
    return {
        "startTimeFrom": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "startTimeTo": window_end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "drilldownTagIds": FOOTBALL_DRILLDOWN_TAG,
        "started": "false",
        "eventSortsIncluded": "MTCH",
        # Both required; see the module docstring.
        "includeChildMarkets": "true",
        "maxMarkets": "1",
        "prioritisePrimaryMarkets": "true",
        "orderMarketsBy": "displayOrder",
        "lang": LOCALE,
        "channel": "I",
    }


def _fetch_once(session: requests.Session) -> list[dict[str, Any]] | None:
    """One fetch attempt. Returns events, or None if the attempt failed.

    An empty list is a valid answer (no fixtures in the window); None means the
    attempt should be retried.
    """
    try:
        response = session.get(LORO_API_URL, params=_request_params())
    except requests.RequestException as exc:
        logger.warning(f"request to Loro failed: {exc}")
        return None

    if response.status_code != 200:
        logger.warning(f"request failed: HTTP {response.status_code}")
        return None

    payload = response.json()

    # The API answers 200 even when it fails, with a null payload and an
    # errors array, so the status code alone is not enough to trust it. The
    # session's urllib3 retries never see this, which is why it is handled here.
    if errors := payload.get("errors"):
        logger.warning(f"Loro returned errors: {errors}")
        return None

    events = (payload.get("data") or {}).get("events")
    if events is None:
        logger.warning("Loro response contained no events")
        return None

    return events


def _fetch_events(session: requests.Session) -> list[dict[str, Any]]:
    """Fetch the football feed, retrying the intermittent backend failure."""
    logger.info("fetching Loro football events...")

    for attempt in range(1, FETCH_ATTEMPTS + 1):
        events = _fetch_once(session)
        if events is not None:
            return events
        if attempt < FETCH_ATTEMPTS:
            delay = FETCH_RETRY_BACKOFF_SECONDS * attempt
            logger.info(f"retrying in {delay:.0f}s ({attempt}/{FETCH_ATTEMPTS})")
            time.sleep(delay)

    logger.critical(f"giving up after {FETCH_ATTEMPTS} attempts")
    return []


def _parse_event(event: dict[str, Any]) -> ScrapedPair | None:
    """Turn one event into a (match, odds) pair, or None if it is unusable."""
    teams = {t["side"]: t["name"] for t in (event.get("teams") or [])}
    if "HOME" not in teams or "AWAY" not in teams:
        logger.warning(f"missing home/away team for event {event.get('id')!r}")
        return None

    match_label = f"{teams['HOME']} vs {teams['AWAY']}"

    match_datetime = datetime.fromisoformat(event["startTime"])
    match_datetime = match_datetime.astimezone(UTC).replace(tzinfo=None)

    market = next(
        (
            m
            for m in (event.get("markets") or [])
            if m.get("groupCode") == MATCH_RESULT_GROUP
        ),
        None,
    )
    if market is None:
        logger.warning(f"no 1X2 market found for {match_label!r}")
        return None

    prices: dict[str, float] = {}
    for outcome in market.get("outcomes") or []:
        side = outcome.get("subType")
        price = (outcome.get("prices") or [{}])[0].get("decimal")
        if side in OUTCOME_SIDES and outcome.get("active") and price is not None:
            prices[side] = float(price)

    if HOME not in prices or AWAY not in prices:
        logger.warning(f"incomplete odds for {match_label!r}: {prices}")
        return None

    try:
        return (
            BookmakerMatchCreate(
                bookmaker=BOOKMAKER,
                match_label=match_label,
                match_datetime=match_datetime,
                team1=teams["HOME"],
                team2=teams["AWAY"],
            ),
            SportsBettingOddsCreate(
                team1_odds=prices[HOME],
                team2_odds=prices[AWAY],
                draw_odds=prices.get(DRAW),
            ),
        )
    except ValidationError as exc:
        logger.warning(f"invalid odds for {match_label!r}, skipping: {exc}")
        return None


def scrape(session: requests.Session) -> list[ScrapedPair]:
    """Fetch the football feed and turn it into (match, odds) pairs."""
    events = _fetch_events(session)
    logger.info(f"found {len(events)} raw events")

    pairs: list[ScrapedPair] = []
    skipped = 0
    for event in events:
        pair = _parse_event(event)
        if pair is None:
            skipped += 1
        else:
            pairs.append(pair)

    logger.info(f"parsed {len(pairs)} matches, skipped {skipped}")
    return pairs


if __name__ == "__main__":
    http = build_session()
    run_from_cli(BOOKMAKER, lambda: scrape(http), session=http, logger=logger)
