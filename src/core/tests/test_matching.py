import pytest

from core.matching import normalise, same_team, team_key

# Real pairs observed in live output from both scrapers. Swisslos writes the
# full club name, Loro a short one.
REAL_PAIRS = [
    ("FC St. Gallen 1879", "St. Gallen"),
    ("FC Thun", "Thun"),
    ("Grasshopper Club Zurich", "Grasshopper"),
    ("Young Boys Bern", "Young Boys"),
    ("Yverdon Sport", "Yverdon"),
    ("Neuchatel Xamax", "Xamax"),
    ("Borussia Dortmund", "Dortmund"),
    ("1. FSV Mainz 05", "Mainz"),
    ("Borussia Monchengladbach", "Monchengladbach"),
    ("Stade Lausanne Ouchy", "Lausanne Ouchy"),
    ("FC Wil 1900", "Wil"),
]

# Different clubs that token containment DOES merge. These are not bugs:
# {mailand} <= {inter, mailand} is structurally identical to
# {grasshopper} <= {grasshopper, zurich}, and the second must match. No rule on
# names alone can separate them, which is exactly why callers must compare only
# fixtures at the same kick-off, where a club appears at most once.
KNOWN_COLLISIONS = [
    ("AC Mailand", "Inter Mailand"),
    ("AS Rom", "Lazio Rom"),
    ("Al Ittihad Kalba", "Al-Ittihad"),
    ("Burton Albion", "Albion FC"),
]


@pytest.mark.parametrize(("full", "short"), REAL_PAIRS)
def test_short_and_full_club_names_are_the_same_team(full, short):
    assert same_team(full, short)
    assert same_team(short, full), "the relation must be symmetric"


@pytest.mark.parametrize(("a", "b"), KNOWN_COLLISIONS)
def test_containment_collides_across_clubs_so_callers_must_block_on_kickoff(a, b):
    """Pins the known limitation, so nobody removes the kick-off block later.

    If this ever starts failing, the rule got stricter and the real pairs above
    should be rechecked — the two behaviours are the same mechanism.
    """
    assert same_team(a, b)


def test_language_variants_of_a_city_match():
    assert same_team("1. FC Cologne", "FC Köln")
    assert same_team("Inter Milano", "Inter Mailand")
    assert same_team("Bayern Munich", "Bayern München")


def test_accents_and_case_are_ignored():
    assert same_team("FC Zürich", "fc zurich")
    assert same_team("Borussia Mönchengladbach", "Monchengladbach")


# ── squad qualifiers ─────────────────────────────────────────────────────────


def test_womens_side_is_not_the_mens_side():
    """Without this rule '1. FC Nürnberg (W)' is contained in 'Nürnberg'."""
    assert not same_team("1. FC Nürnberg (W)", "Nürnberg")
    assert not same_team("FC Zurich F", "FC Zurich")


def test_two_womens_sides_still_match_each_other():
    assert same_team("FC Zurich (W)", "Zurich (W)")


def test_reserve_and_youth_sides_are_distinct():
    assert not same_team("FC Basel II", "FC Basel")
    assert not same_team("Young Boys U21", "Young Boys")


# ── degenerate input ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("name", ["", "FC", "SC 1899", "   "])
def test_names_with_no_identifying_tokens_never_match(name):
    """A name that normalises to nothing must not match everything."""
    assert not same_team(name, "Thun")
    assert not same_team("Thun", name)


def test_normalise_separates_core_from_qualifiers():
    core, qualifiers = normalise("1. FC Nürnberg (W)")
    assert core == {"nurnberg"}
    assert qualifiers == {"w"}


# ── keys ─────────────────────────────────────────────────────────────────────


def test_team_key_is_stable_across_decoration():
    assert team_key("FC St. Gallen 1879") == team_key("St. Gallen")
    assert team_key("FC Thun") == team_key("Thun")


def test_team_key_distinguishes_squads():
    assert team_key("FC Zurich (W)") != team_key("FC Zurich")


def test_team_key_is_order_independent():
    assert team_key("Lausanne Ouchy") == team_key("Ouchy Lausanne")


# ── abbreviations ────────────────────────────────────────────────────────────


def test_dotted_abbreviations_are_the_undotted_ones():
    """'F.C.' must read as the legal form, not as the women's qualifier 'F'."""
    assert same_team("F.C. Basel", "FC Basel")
    assert same_team("Servette F.C.", "Servette")
    assert team_key("F.C. Basel") == team_key("FC Basel")
    assert team_key("A.C. Milan") == team_key("AC Mailand")


def test_an_initial_is_not_a_squad_qualifier():
    """'W. Bremen' is Werder, not a women's side; 'B. Dortmund' is the first team."""
    assert normalise("W. Bremen")[1] == frozenset()
    assert normalise("B. Dortmund")[1] == frozenset()
    assert same_team("B. Dortmund", "Borussia Dortmund")


def test_a_trailing_letter_is_still_a_qualifier():
    """The rule above must not undo how both feeds actually mark squads."""
    assert normalise("FC Zurich F")[1] == {"f"}
    assert normalise("Bâle F")[1] == {"f"}
    assert not same_team("Bâle F", "FC Basel")
