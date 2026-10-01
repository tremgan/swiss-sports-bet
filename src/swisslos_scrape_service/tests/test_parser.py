"""Parser tests against the frame shape Swisslos actually sends."""

from main import parse_messages


def frame(entities: list[dict]) -> dict:
    """Wrap entities in the envelope `parse_messages` unwraps."""
    return {
        "payload": [{"body": {"snapshotUpdate": {"snapshotUpdateItems": entities}}}]
    }


def competitor(urn: str, name: str, de: str | None = None) -> dict:
    translations = {"en": name}
    if de:
        translations["de"] = de
    return {
        "type": "Competitor",
        "entity": {"urn": urn, "name": name, "translations": translations},
    }


def one_x_two(event_urn: str, home: str, away: str) -> list[dict]:
    market_urn = f"{event_urn}/market"
    selections = [
        {
            "type": "Selection",
            "entity": {
                "urn": f"{market_urn}/{role}",
                "type": sel_type,
                "odds": odds,
            },
        }
        for role, sel_type, odds in (
            ("home", "asw:selectiontype:1", 2.1),
            ("draw", "asw:selectiontype:2", 3.6),
            ("away", "asw:selectiontype:3", 4.0),
        )
    ]
    return [
        *selections,
        {
            "type": "Market",
            "entity": {
                "urn": market_urn,
                "type": "asw:markettype:1",
                "selections": [s["entity"]["urn"] for s in selections],
            },
        },
        {
            "type": "Event",
            "entity": {
                "urn": event_urn,
                "startTime": "2026-10-01T18:45:00Z",
                "eventCompetitors": [{"competitor": home}, {"competitor": away}],
                "markets": [market_urn],
            },
        },
    ]


def test_german_competitor_name_is_preferred_over_the_english_one():
    """Loro is scraped in de-CH, so "Germany" would never link to "Deutschland"."""
    messages = [
        frame(
            [
                competitor("c:1", "Germany", "Deutschland"),
                competitor("c:2", "Serbia", "Serbien"),
                *one_x_two("e:1", "c:1", "c:2"),
            ]
        )
    ]

    ((match, odds),) = parse_messages(messages)

    assert (match.team1, match.team2) == ("Deutschland", "Serbien")
    assert match.match_label == "Deutschland vs Serbien"
    assert (odds.team1_odds, odds.draw_odds, odds.team2_odds) == (2.1, 3.6, 4.0)


def test_falls_back_to_the_plain_name_when_there_is_no_german_form():
    """Portugal, Israel, Malta and the like ship no `de` entry."""
    messages = [
        frame(
            [
                competitor("c:1", "Portugal"),
                competitor("c:2", "Israel"),
                *one_x_two("e:1", "c:1", "c:2"),
            ]
        )
    ]

    ((match, _),) = parse_messages(messages)

    assert (match.team1, match.team2) == ("Portugal", "Israel")
