"""Disqualifiers for handsets that cannot legally or practically be resold.

Every string in here is copied verbatim from a live Vinted listing. That matters
more than usual: the first version of these rules keyed on the word "icloud",
and the listing that got through said

    "Locked and I don't know the password but would be good once gets unlocked
     by professional"

which is an activation-locked handset described without ever using the word.
The verifier passed it as ok=True at £106 against a £149 CeX floor, so it read
as the single best margin on the board while being worth close to nothing.

The opposite error matters just as much. "Unlocked" is the most common word in
this category and means the phone is *fine*; a rule that treats it as a lock
would silence the whole feed. Both directions are pinned below.
"""

from __future__ import annotations

import pytest

from phoneloop.parser import parse
from phoneloop.verifier import count_photos

# --- verbatim from live listings, 2026-08-27 -------------------------------

# Vinted item 9790839626, re-fetched 2026-08-27. Note the apostrophe: it is
# U+2019, not ASCII. That is what Vinted's own composer produces, so it is what
# the rules actually face. An earlier version of this fixture quietly retyped it
# as a straight quote while claiming to be verbatim -- the same shortcut that
# once let a photo counter return zero for every real listing and pass its own
# test. parser._normalise folds it; this fixture is what proves that.
LOCKED_NO_PASSWORD = (
    "Locked and I don’t know the password but would be good once gets "
    "unlocked by professional"
)


class TestActivationLock:
    def test_the_listing_that_got_through(self):
        # The regression this file exists for.
        spec = parse("iPhone 14", LOCKED_NO_PASSWORD)
        assert "activation_locked" in spec.disqualifiers

    def test_a_curly_apostrophe_does_not_defeat_the_rule(self):
        """LOCKED_NO_PASSWORD would still be caught if normalisation broke,
        because it also says "Locked ... password" and a second clause covers
        that. This one has no such safety net: the only thing standing between
        it and a false pass is parser._normalise folding U+2019.
        """
        text = "I don’t know the passcode, selling as spares"
        assert "’" in text, "the point of this test is the curly quote"
        assert "activation_locked" in parse("iPhone 12", text).disqualifiers

    @pytest.mark.parametrize("text", [
        "locked and i don't know the password",
        "I dont know the passcode sorry",
        "forgot the apple id and password",
        "still linked to previous owners icloud",
        "locked to previous owner",
        "needs unlocking, apple id lock",
        "activation lock on it",
        "find my iphone is on",
        "iphone is disabled, cannot get in",
    ])
    def test_phrasings_that_all_mean_you_cannot_use_it(self, text):
        assert "activation_locked" in parse("iPhone 12", text).disqualifiers

    @pytest.mark.parametrize("text", [
        "Unlocked to all networks",
        "factory unlocked, works perfectly",
        "network unlocked",
        "fully unlocked and reset to factory settings",
        "unlocked, no icloud, ready to use",
    ])
    def test_unlocked_is_not_a_lock(self, text):
        # \blocked\b does not match inside "unlocked" -- pinned so a future
        # loosening of the pattern cannot silently mute the whole feed.
        assert "activation_locked" not in parse("iPhone 12", text).disqualifiers


class TestBlacklisted:
    @pytest.mark.parametrize("text", [
        "sold as blacklisted",
        "imei is blocked unfortunately",
        "reported lost so barred from networks",
        "phone is on the blacklist",
    ])
    def test_a_barred_imei_is_disqualifying(self, text):
        assert "blacklisted" in parse("iPhone 13", text).disqualifiers

    def test_a_clean_imei_claim_is_not(self):
        spec = parse("iPhone 13", "imei clean, not blacklisted, check before buying")
        assert "blacklisted" not in spec.disqualifiers


class TestTitleStillWorksAlone:
    def test_disqualifiers_still_read_from_the_title(self):
        assert "activation_locked" in parse("iPhone 11 icloud locked").disqualifiers

    def test_a_clean_listing_stays_clean(self):
        spec = parse("iPhone 13 Pro 256GB unlocked", "Great condition, boxed")
        assert spec.disqualifiers == ()
        assert spec.model == "iphone-13-pro"


class TestPhotoCount:
    """The page carries photos as HTML attributes, not JSON keys.

    The original pattern looked for `"full_size_url":` / `"url":` before an
    image URL. On a real page exactly one of 186 image URLs has a JSON key in
    front of it, so every listing counted zero photos -- which quietly disabled
    the "one stock image vs eight real photos" risk signal entirely.
    """

    def test_counts_distinct_images_not_srcset_repeats(self):
        html = " ".join(
            f'<img src="https://images1.vinted.net/t/{i}_abc/f800/{i}.webp" '
            f'srcset="https://images1.vinted.net/t/{i}_abc/f400/{i}.webp 1x, '
            f'https://images1.vinted.net/t/{i}_abc/f800/{i}.webp 2x">'
            for i in range(5)
        )
        assert count_photos(html) == 5

    def test_a_single_photo_listing_counts_one(self):
        html = ('<img src="https://images1.vinted.net/t/01_xyz/f800/9.webp">'
                '<img src="https://images1.vinted.net/t/01_xyz/f400/9.webp">')
        assert count_photos(html) == 1

    def test_no_photos_is_zero_not_an_error(self):
        assert count_photos("<html><body>nothing here</body></html>") == 0


class TestBundleDoesNotEatModelNames:
    """`\bx\d+\b` was meant to catch "x3" as a quantity. It also matches the
    X9 in "Oppo Find X9", which silently threw away a £788 listing -- found by
    looking at the pile the scanner had discarded.
    """

    @pytest.mark.parametrize("title", [
        "Oppo Find X9 Pro 16/512GB Titanium Charcoal",
        "Poco X6 Pro 512GB",
        "Nothing Phone X2 256GB",
    ])
    def test_a_model_name_containing_x_plus_digit_is_not_a_bundle(self, title):
        assert "bundle" not in parse(title).disqualifiers

    @pytest.mark.parametrize("title", [
        "4x Sony Xperia Z3 BUNDLE",
        "3 x iPhone 11 job lot",
        "iPhone cases x3",
        "Samsung chargers x10",
    ])
    def test_real_quantities_are_still_caught(self, title):
        assert "bundle" in parse(title).disqualifiers
