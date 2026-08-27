"""Damage words, and the negations that make them harmless.

Every positive here is copied verbatim from a live Vinted listing. The one this
file exists for is the last line of

    "iPhone 14 Pro Max.
     Perfect working condition
     Nothing wrong with the phone itself but has a crack on the back as shown."

which the rules passed as CLEAN because they matched "cracked" and not "crack".
It then ranked FIRST on the board, as the only guaranteed-safe buy the project
had ever surfaced: 189.69 to buy against a 206 CeX floor.

The opposite error is just as expensive. "No cracks or scratches" is one of the
commonest sentences in a genuinely clean listing, and a rule that reads it as
damage bins most of the good stock. Both directions are pinned.
"""

from __future__ import annotations

import pytest

from phoneloop.parser import parse

TITLE = "iPhone 14 Pro Max"

# --- verbatim from live listings, 2026-08-27 --------------------------------

CRACK_NOUN = (
    "iPhone 14 Pro Max. \nPerfect working condition\nNothing wrong with the "
    "phone itself but has a crack on the back as shown."
)
SMASHED_BACK = "Smashed back but fully working"
CRACKED_SCREENS = (
    "Fully functional, just smashed back screen and front screen is slightly "
    "cracked"
)


class TestTheListingThatGotThrough:
    def test_a_crack_is_damage_even_as_a_noun(self):
        assert "damaged" in parse(TITLE, CRACK_NOUN).disqualifiers

    def test_the_other_two_still_caught(self):
        assert "damaged" in parse(TITLE, SMASHED_BACK).disqualifiers
        assert "damaged" in parse(TITLE, CRACKED_SCREENS).disqualifiers


class TestNounForms:
    @pytest.mark.parametrize("text", [
        "has a crack on the back",
        "small crack in the corner",
        "a few cracks along the edge",
        "there is a chip on the frame",
        "some chips near the camera",
        "a dent on the side",
        "dents along the bottom edge",
        "spider web crack across the screen",
        "screen burn visible on white backgrounds",
    ])
    def test_damage_described_as_a_thing_not_an_adjective(self, text):
        assert "damaged" in parse(TITLE, text).disqualifiers, text


class TestNegationsMustNotDisqualify:
    """A rule that bins these bins the entire clean half of the feed."""

    @pytest.mark.parametrize("text", [
        "no cracks",
        "no cracks or scratches",
        "no chips or dents",
        "not cracked",
        "never cracked or repaired",
        "without any cracks",
        "free from cracks and chips",
        "zero cracks",
        "screen has no crack at all",
        "nothing cracked",
    ])
    def test_a_reassurance_is_not_damage(self, text):
        assert "damaged" not in parse(TITLE, text).disqualifiers, text


class TestNegationScope:
    """A negation governs its own clause and no further."""

    def test_a_negation_about_something_else_does_not_excuse_a_crack(self):
        """The "no" is about the charger. The crack is still a crack."""
        spec = parse(TITLE, "no charger included, cracked back")
        assert "damaged" in spec.disqualifiers

    def test_a_clean_claim_followed_by_a_real_admission_still_fails(self):
        """The commonest shape of a misleading listing: reassure, then admit.
        The first damage word is negated and the second is not, so the scan
        cannot stop at the first match."""
        spec = parse(TITLE, "no scratches at all but the back is cracked")
        assert "damaged" in spec.disqualifiers

    def test_a_full_stop_ends_the_negation(self):
        spec = parse(TITLE, "No damage. Crack on the rear glass.")
        assert "damaged" in spec.disqualifiers

    def test_a_negation_far_away_does_not_reach(self):
        spec = parse(TITLE, "no issues whatsoever with the battery or the "
                            "charging port at any point crack on the back")
        assert "damaged" in spec.disqualifiers


class TestOtherRulesKeepTheirNegations:
    def test_not_blacklisted_is_not_blacklisted(self):
        assert "blacklisted" not in parse(TITLE, "not blacklisted").disqualifiers

    def test_no_icloud_lock_is_not_a_lock(self):
        assert "activation_locked" not in parse(
            TITLE, "no icloud lock, signed out").disqualifiers

    def test_a_genuine_lock_still_fails(self):
        assert "activation_locked" in parse(
            TITLE, "icloud locked, no password").disqualifiers
