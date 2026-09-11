"""Deciding when two bookmakers mean the same team.

Bookmakers disagree about how much of a club's name to write down. Swisslos
says "Grasshopper Club Zurich" where Loro says "Grasshopper"; one says
"Cologne" where the other says "Köln". Rather than score the similarity of two
strings and pick a threshold, this module reduces a name to the tokens that
carry identity and asks whether one name's tokens are contained in the other's.

Two rules make that safe:

- **Containment is only valid inside a kick-off block.** Applied across a whole
  feed it merges genuinely different clubs — "AC Mailand" into "Inter Mailand",
  "AS Rom" into "Lazio Rom". Callers must compare only fixtures kicking off at
  the same time, where a club can appear at most once.
- **Squad qualifiers are compared, never absorbed.** Without this, "1. FC
  Nürnberg (W)" is contained in "Nürnberg" and a women's side merges into the
  men's.
"""

import re
import unicodedata

# Club decoration that carries no identity: legal forms, generic words, and the
# sponsor-ish prefixes bookmakers add or drop freely.
DECORATION = frozenset(
    {
        "fc",
        "sc",
        "bsc",
        "ac",
        "as",
        "afc",
        "cf",
        "sv",
        "vfb",
        "vfl",
        "tsg",
        "rb",
        "fsv",
        "ssc",
        "us",
        "ud",
        "cd",
        "sd",
        "rc",
        "fk",
        "nk",
        "hk",
        "club",
        "calcio",
        "futbol",
        "football",
        "team",
        "sport",
        "sports",
        "borussia",
        "stade",
        "olympique",
        "sl",
        "sk",
        "ik",
        "if",
    }
)

# A squad qualifier distinguishes a women's, youth or reserve side from the
# senior men's side of the same club. Compared for equality, never stripped.
QUALIFIERS = frozenset(
    {
        # Loro marks women's sides with a bare "F" ("FC Zurich F", "Bâle F").
        "f",
        "w",
        "women",
        "womens",
        "frauen",
        "femminile",
        "feminin",
        "feminine",
        "ladies",
        "lfc",
        "wfc",
        "ii",
        "b",
        "u19",
        "u20",
        "u21",
        "u23",
        "reserve",
        "reserves",
    }
)

# The same city in different languages. Bookmakers localise inconsistently.
EXONYMS = {
    "cologne": "koln",
    "koeln": "koln",
    "milano": "mailand",
    "milan": "mailand",
    "munich": "munchen",
    "muenchen": "munchen",
    "turin": "torino",
    "geneve": "genf",
    "geneva": "genf",
    "zuerich": "zurich",
    "zurigo": "zurich",
    "basle": "basel",
    "bale": "basel",
    "vienna": "wien",
    "prague": "praha",
    "warsaw": "warszawa",
}

# Founding years and squad numbers: "1. FC Köln", "FC St. Gallen 1879".
_NUMERIC = re.compile(r"^\d{1,4}$")

# A run of two or more single letters each followed by a period is one
# abbreviation: "F.C." is the decoration "fc", not the qualifier "f" beside a
# stray "c". A lone "St." or "1." does not match and keeps its own token.
_DOTTED_ABBREVIATION = re.compile(r"\b(?:[a-z]\.){2,}")


def _words(name: str) -> list[str]:
    """Lowercase, strip accents and punctuation, and apply exonyms."""
    folded = unicodedata.normalize("NFKD", name.lower()).replace("ß", "ss")
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    folded = _DOTTED_ABBREVIATION.sub(lambda m: m.group().replace(".", ""), folded)
    folded = re.sub(r"[^a-z0-9 ]", " ", folded)
    return [EXONYMS.get(w, w) for w in folded.split()]


def _is_qualifier(word: str, index: int, last: int) -> bool:
    """Whether a word marks a squad, rather than merely looking like one.

    Both feeds write a one-letter qualifier last ("FC Zurich F", "Bâle F",
    "1. FC Nürnberg (W)"). Elsewhere a lone letter is an initial — "W. Bremen",
    "B. Dortmund" — and reading it as a squad marker would split a men's side
    off from itself. Spelt-out qualifiers are unambiguous in any position.
    """
    if word not in QUALIFIERS:
        return False
    return len(word) > 1 or index == last


def normalise(name: str) -> tuple[frozenset[str], frozenset[str]]:
    """Split a team name into its identifying tokens and its squad qualifiers."""
    words = _words(name)
    last = len(words) - 1
    core: set[str] = set()
    qualifiers: set[str] = set()
    for index, word in enumerate(words):
        if _is_qualifier(word, index, last):
            qualifiers.add(word)
        elif word not in DECORATION and not _NUMERIC.match(word):
            core.add(word)
    return frozenset(core), frozenset(qualifiers)


def team_key(name: str) -> str:
    """A stable string form of a name's identity, for storage and exact lookup."""
    core, qualifiers = normalise(name)
    key = " ".join(sorted(core))
    if qualifiers:
        key = f"{key} ({' '.join(sorted(qualifiers))})"
    return key


def same_team(a: str, b: str) -> bool:
    """Whether two names denote the same team.

    Only meaningful between fixtures at the same kick-off — see the module
    docstring for why containment is unsafe across a whole feed.
    """
    core_a, qual_a = normalise(a)
    core_b, qual_b = normalise(b)

    if qual_a != qual_b:
        return False
    if not core_a or not core_b:
        return False
    return core_a <= core_b or core_b <= core_a
