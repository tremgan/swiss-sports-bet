import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
import requests

import main

FIXTURE = Path(__file__).parent / "fixtures" / "event_list.json"


@pytest.fixture(name="payload")
def payload_fixture() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text())


class FakeResponse:
    def __init__(self, payload: dict[str, Any], status_code: int = 200):
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict[str, Any]:
        return self._payload


class FakeSession(requests.Session):
    """Replays queued payloads (or exceptions) instead of calling Loro."""

    def __init__(self, *results):
        super().__init__()
        self._results = list(results)
        self.calls: list[dict[str, Any]] = []

    def get(self, url, **kwargs):  # type: ignore[override]
        self.calls.append(kwargs.get("params") or {})
        result = self._results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def scrape_fixture(payload: dict[str, Any]):
    return main.scrape(FakeSession(FakeResponse(payload)))


def by_label(pairs):
    return {match.match_label: odds for match, odds in pairs}


# ── the happy path ────────────────────────────────────────────────────────────


def test_parses_a_three_way_market(payload):
    odds = by_label(scrape_fixture(payload))["St. Gallen vs Sion"]

    # subType H/D/A -> team1/draw/team2, independent of the localised names.
    assert odds.team1_odds == 2.05
    assert odds.draw_odds == 3.75
    assert odds.team2_odds == 2.8


def test_builds_the_label_from_home_and_away_sides(payload):
    labels = [m.match_label for m, _ in scrape_fixture(payload)]

    # Not a split of event["name"], which uses " v " rather than " vs ".
    assert "St. Gallen vs Sion" in labels


def test_bookmaker_is_tagged(payload):
    assert all(m.bookmaker == "Loro" for m, _ in scrape_fixture(payload))


def test_kick_off_is_converted_to_naive_utc(payload):
    match = next(
        m for m, _ in scrape_fixture(payload) if m.match_label.startswith("St.")
    )

    assert match.match_datetime == datetime(2026, 9, 12, 16, 0)
    assert match.match_datetime.tzinfo is None


def test_two_way_market_yields_no_draw(payload):
    two_way = [
        odds
        for match, odds in scrape_fixture(payload)
        if match.match_label not in ("St. Gallen vs Sion",) and odds.draw_odds is None
    ]

    assert len(two_way) == 1
    assert two_way[0].team1_odds is not None


# ── events that must be skipped, not fatal ───────────────────────────────────


def test_skips_malformed_events_without_failing_the_run(payload):
    pairs = scrape_fixture(payload)

    # 5 events in, 3 skipped: no teams, no 1X2 market, suspended home price.
    assert len(pairs) == 2


@pytest.mark.parametrize(
    ("event_id", "reason"),
    [("no-teams", "missing sides"), ("no-1x2", "no MATCH_RESULT market")],
)
def test_specific_malformed_event_is_dropped(payload, event_id, reason):
    only = [e for e in payload["data"]["events"] if e["id"] == event_id]
    payload["data"]["events"] = only

    assert scrape_fixture(payload) == [], reason


def test_suspended_outcome_is_not_priced(payload):
    """An inactive outcome leaves the market incomplete, so the event is dropped."""
    payload["data"]["events"] = [
        e for e in payload["data"]["events"] if e["id"] == "suspended"
    ]

    assert scrape_fixture(payload) == []


# ── transport and backend failures ───────────────────────────────────────────


def test_backend_error_body_returns_empty_rather_than_raising(monkeypatch):
    """The API answers 200 with an errors array; that must not look like success."""
    monkeypatch.setattr(main, "FETCH_RETRY_BACKOFF_SECONDS", 0)
    error_body = {
        "data": {"events": None},
        "errors": [{"type": "DataFetchingException", "message": "..."}],
    }
    session = FakeSession(*[FakeResponse(error_body)] * main.FETCH_ATTEMPTS)

    assert main.scrape(session) == []
    assert len(session.calls) == main.FETCH_ATTEMPTS  # retried, not given up on


def test_transient_backend_error_is_retried_then_succeeds(payload, monkeypatch):
    monkeypatch.setattr(main, "FETCH_RETRY_BACKOFF_SECONDS", 0)
    error_body = {
        "data": {"events": None},
        "errors": [{"type": "DataFetchingException"}],
    }
    session = FakeSession(FakeResponse(error_body), FakeResponse(payload))

    assert len(main.scrape(session)) == 2
    assert len(session.calls) == 2


def test_network_error_returns_empty(monkeypatch):
    monkeypatch.setattr(main, "FETCH_RETRY_BACKOFF_SECONDS", 0)
    session = FakeSession(*[requests.ConnectionError("boom")] * main.FETCH_ATTEMPTS)

    assert main.scrape(session) == []


def test_non_200_returns_empty(monkeypatch):
    monkeypatch.setattr(main, "FETCH_RETRY_BACKOFF_SECONDS", 0)
    session = FakeSession(*[FakeResponse({}, status_code=500)] * main.FETCH_ATTEMPTS)

    assert main.scrape(session) == []


# ── the request itself ───────────────────────────────────────────────────────


def test_request_sends_the_parameters_the_api_requires(payload):
    session = FakeSession(FakeResponse(payload))
    main.scrape(session)
    params = session.calls[0]

    # Without these two the response carries either no markets at all or every
    # market for every event (~120MB per run).
    assert params["includeChildMarkets"] == "true"
    assert params["maxMarkets"] == "1"
    assert params["prioritisePrimaryMarkets"] == "true"
    assert params["drilldownTagIds"] == main.FOOTBALL_DRILLDOWN_TAG
    # eventState=OPEN_EVENT makes the backend throw; it must never be sent.
    assert "eventState" not in params
