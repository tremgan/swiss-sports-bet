import logging
from datetime import datetime
from typing import Any, cast

import pytest
import requests
from core.models import BookmakerMatchCreate, SportsBettingOddsCreate
from core.scraper import build_session, publish, run_from_cli
from requests.adapters import HTTPAdapter

DB_URL = "http://db-service.test"
logger = logging.getLogger("test")


class FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = str(self._payload)

    def json(self) -> dict:
        return self._payload


class FakeSession(requests.Session):
    """Replays a queued list of responses (or exceptions) and records calls."""

    def __init__(self, responses):
        super().__init__()
        self._responses = list(responses)
        self.calls: list[tuple[str, Any]] = []

    def post(self, url, data=None, json=None, **kwargs):
        self.calls.append((str(url), json))
        result = self._responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def make_pair(label: str = "A vs B"):
    return (
        BookmakerMatchCreate(
            bookmaker="Loro",
            match_label=label,
            match_datetime=datetime(2026, 3, 25, 18, 0),  # noqa: DTZ001
            team1=label.split(" vs ")[0],
            team2=label.split(" vs ")[1],
        ),
        SportsBettingOddsCreate(team1_odds=2.1, draw_odds=3.6, team2_odds=4.0),
    )


def test_publish_posts_match_then_odds_and_links_them():
    session = FakeSession([FakeResponse(200, {"id": 42}), FakeResponse(200)])
    pair = make_pair()

    posted, failed = publish([pair], DB_URL, session, logger)

    assert (posted, failed) == (1, 0)
    match_url, match_body = session.calls[0]
    odds_url, odds_body = session.calls[1]
    assert match_url == f"{DB_URL}/bookmaker_matches/"
    assert match_body["match_label"] == "A vs B"
    assert odds_url == f"{DB_URL}/sports_betting_odds/"
    # The id returned for the match must be attached to its odds.
    assert odds_body["bookmaker_match_id"] == 42


def test_publish_counts_a_failed_match_post_and_skips_its_odds():
    session = FakeSession([FakeResponse(422, {"detail": "nope"})])

    posted, failed = publish([make_pair()], DB_URL, session, logger)

    assert (posted, failed) == (0, 1)
    assert len(session.calls) == 1  # odds were never attempted


def test_publish_counts_a_failed_odds_post():
    session = FakeSession([FakeResponse(200, {"id": 1}), FakeResponse(500)])

    posted, failed = publish([make_pair()], DB_URL, session, logger)

    assert (posted, failed) == (0, 1)


def test_publish_survives_a_network_error_and_continues():
    session = FakeSession(
        [
            requests.ConnectionError("boom"),
            FakeResponse(200, {"id": 7}),
            FakeResponse(200),
        ]
    )

    pairs = [make_pair("A vs B"), make_pair("C vs D")]
    posted, failed = publish(pairs, DB_URL, session, logger)

    assert (posted, failed) == (1, 1)


def test_build_session_applies_a_default_timeout_and_user_agent():
    session = build_session()
    assert "swiss-sports-bet" in session.headers["User-Agent"]

    captured = {}

    def fake_send(request, **kwargs):
        captured.update(kwargs)
        raise requests.ConnectionError("stop here")

    session.send = fake_send
    with pytest.raises(requests.ConnectionError):
        session.get("http://example.test")

    assert captured["timeout"] == pytest.approx(30.0)


def test_build_session_retries_get_but_not_post_on_server_errors():
    """POST /sports_betting_odds/ is not idempotent, so 5xx must not be replayed."""
    adapter = build_session().get_adapter("https://example.test")
    retry = cast(HTTPAdapter, adapter).max_retries

    assert retry.is_retry("GET", 503) is True
    assert retry.is_retry("POST", 503) is False
    assert retry.connect == 3


def test_run_from_cli_runs_one_cycle_with_once_and_does_not_loop():
    session = FakeSession([FakeResponse(200, {"id": 1}), FakeResponse(200, {"id": 9})])
    calls: list[int] = []

    def scrape_fn():
        calls.append(1)
        return [make_pair()]

    run_from_cli("Loro", scrape_fn, session=session, logger=logger, argv=["--once"])

    assert calls == [1]
    assert [url for url, _ in session.calls] == [
        "http://127.0.0.1:8000/bookmaker_matches/",
        "http://127.0.0.1:8000/sports_betting_odds/",
    ]


def test_run_from_cli_without_once_runs_forever(monkeypatch: pytest.MonkeyPatch):
    """Without the flag it is the daemon, so a scheduled job must pass --once."""
    seen: dict[str, Any] = {}

    def fake_run_forever(bookmaker, scrape_fn, **kwargs):
        seen["bookmaker"] = bookmaker

    monkeypatch.setattr("core.scraper.run_forever", fake_run_forever)
    run_from_cli("Loro", list, logger=logger, argv=[])

    assert seen["bookmaker"] == "Loro"
