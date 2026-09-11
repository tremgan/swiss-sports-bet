"""Shared runtime for the bookmaker scrapers.

Every scraper does the same thing once it has parsed its source: POST each
match, attach the returned id to its odds, POST those, then ask the db_service
to reconcile. Only the parsing differs, so a scraper here is just a callable
returning `(match, odds)` pairs.

Returning *pairs* rather than two parallel lists is deliberate: the previous
per-service copies of this loop kept `matches` and `odds` aligned by hand and
popped the last element on a validation error, which desynchronised them the
moment the error came from the match rather than the odds.
"""

import logging
import os
import time
from collections.abc import Callable

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from core.models import BookmakerMatchCreate, SportsBettingOddsCreate

ScrapedPair = tuple[BookmakerMatchCreate, SportsBettingOddsCreate]
ScrapeFn = Callable[[], list[ScrapedPair]]

DEFAULT_TIMEOUT_SECONDS = 30.0
USER_AGENT = "swiss-sports-bet/0.1 (+https://github.com/tremgan/sports-bet)"
SECONDS_PER_HOUR = 3600


def db_service_url() -> str:
    return os.getenv("DB_SERVICE_URL", "http://127.0.0.1:8000")


def scrape_frequency_hours() -> int:
    return int(os.getenv("SCRAPE_FREQUENCY_HOURS", "3"))


class _TimeoutSession(requests.Session):
    """A Session applying a default timeout to every request.

    requests has no global timeout setting and defaults to waiting forever,
    which is fatal for a daemon whose entire job is network I/O.
    """

    def __init__(self, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> None:
        super().__init__()
        self.timeout = timeout

    def request(self, method, url, **kwargs):  # type: ignore[override]
        kwargs.setdefault("timeout", self.timeout)
        return super().request(method, url, **kwargs)


def build_session(
    user_agent: str = USER_AGENT,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> requests.Session:
    """A pooled session with timeouts, a real user agent and careful retries."""
    session = _TimeoutSession(timeout)
    session.headers["User-Agent"] = user_agent

    retry = Retry(
        total=3,
        connect=3,
        read=0,
        status=3,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        # Status retries are GET-only on purpose. POST /sports_betting_odds/
        # inserts a new timestamped row on every call, so replaying a 5xx there
        # could double-insert. Connect retries stay enabled for every method,
        # because a connection error means the request never reached the server.
        allowed_methods=("GET",),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def publish(
    pairs: list[ScrapedPair],
    db_service_url: str,
    session: requests.Session,
    logger: logging.Logger,
) -> tuple[int, int]:
    """POST each pair to the db_service, returning `(posted, failed)` counts."""
    posted = 0
    failed = 0

    for match, odds in pairs:
        try:
            match_response = session.post(
                f"{db_service_url}/bookmaker_matches/",
                json=match.model_dump(mode="json"),
            )
        except requests.RequestException as exc:
            logger.error(f"posting match {match.match_label!r} failed: {exc}")
            failed += 1
            continue

        if match_response.status_code != 200:
            logger.error(
                f"failed to post match {match.match_label!r}: {match_response.text}"
            )
            failed += 1
            continue

        odds.bookmaker_match_id = match_response.json().get("id")

        try:
            odds_response = session.post(
                f"{db_service_url}/sports_betting_odds/",
                json=odds.model_dump(mode="json"),
            )
        except requests.RequestException as exc:
            logger.error(f"posting odds for {match.match_label!r} failed: {exc}")
            failed += 1
            continue

        if odds_response.status_code != 200:
            logger.error(
                f"failed to post odds for {match.match_label!r}: {odds_response.text}"
            )
            failed += 1
        else:
            posted += 1

    return posted, failed


def trigger_matching(
    db_service_url: str,
    session: requests.Session,
    logger: logging.Logger,
) -> bool:
    """Ask the db_service to reconcile bookmaker matches. True if it succeeded."""
    try:
        response = session.post(f"{db_service_url}/run_matching/")
    except requests.RequestException as exc:
        logger.error(f"could not trigger match-making: {exc}")
        return False

    if response.status_code != 200:
        logger.error(f"match-making failed: HTTP {response.status_code}")
        return False

    logger.info("match-making complete")
    return True


def run_once(
    bookmaker: str,
    scrape_fn: ScrapeFn,
    *,
    db_service_url: str,
    session: requests.Session,
    logger: logging.Logger,
) -> None:
    """Scrape, publish and reconcile once, absorbing any error."""
    logger.info(f"=== {bookmaker} scrape run starting ===")
    try:
        started = time.perf_counter()
        pairs = scrape_fn()
        elapsed = time.perf_counter() - started
        logger.info(f"scraped {len(pairs)} matches in {elapsed:.2f}s")

        posted, failed = publish(pairs, db_service_url, session, logger)
        logger.info(f"posted {posted} matches, {failed} failures")

        if posted:
            trigger_matching(db_service_url, session, logger)
    except Exception:
        # The daemon has to survive a bad run; the next cycle starts clean.
        logger.exception(f"unhandled error during {bookmaker} scrape")

    logger.info(f"=== {bookmaker} scrape run complete ===")


def run_forever(
    bookmaker: str,
    scrape_fn: ScrapeFn,
    *,
    frequency_hours: int | None = None,
    url: str | None = None,
    session: requests.Session | None = None,
    logger: logging.Logger | None = None,
) -> None:
    """Run `scrape_fn` on a fixed interval until the process is stopped."""
    frequency_hours = (
        scrape_frequency_hours() if frequency_hours is None else frequency_hours
    )
    url = db_service_url() if url is None else url
    session = build_session() if session is None else session
    logger = logging.getLogger(bookmaker) if logger is None else logger

    logger.info(f"starting {bookmaker} scraper, frequency: {frequency_hours}h")
    while True:
        run_once(
            bookmaker, scrape_fn, db_service_url=url, session=session, logger=logger
        )
        logger.info(f"sleeping {frequency_hours}h until next run")
        time.sleep(frequency_hours * SECONDS_PER_HOUR)
