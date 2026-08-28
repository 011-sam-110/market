"""Identity tiers, product keys, and title-shingle clustering (spec section
5, contracts section 2.9).

Rows below are literal dicts built from the ROW_FIELDS column list in
contracts section 1.1 -- only the columns each test actually reads are set;
everything else is equivalent to the NULL `market_store.empty_row()` would
produce for it.
"""

from __future__ import annotations

import pytest

from phoneloop.identity import (
    cluster,
    is_junk_mpn,
    jaccard,
    product_key,
    shingles,
    tier_for,
)

LEAF = "139971"


class TestJunkMpn:
    def test_the_measured_junk_example(self):
        """The exact string from spec section 5 / contracts section 2.5:
        a seller typed a description, not a part number."""
        assert is_junk_mpn("Wireless-charge-carphonr") is True

    def test_a_real_mpn_with_a_digit_in_one_token_is_not_junk(self):
        assert is_junk_mpn("SM-G991B") is False

    def test_a_single_token_mpn_is_not_junk_even_with_no_digit(self):
        assert is_junk_mpn("PRO") is False

    def test_a_compact_alphanumeric_mpn_is_not_junk(self):
        assert is_junk_mpn("A2482") is False

    def test_none_is_not_junk(self):
        assert is_junk_mpn(None) is False

    def test_empty_string_is_not_junk(self):
        assert is_junk_mpn("") is False

    def test_another_multiword_all_alpha_mpn_is_junk(self):
        assert is_junk_mpn("Fast-charging-cable") is True


class TestTierFor:
    def test_catalogued_from_a_true_epid(self):
        row = {"epid": "epid123", "gtin": None, "identity_tier": None}
        assert tier_for(row) == "catalogued"

    def test_catalogued_from_a_true_epid_explicitly_marked_catalogued(self):
        row = {"epid": "epid123", "gtin": None, "identity_tier": "catalogued"}
        assert tier_for(row) == "catalogued"

    def test_catalogued_from_a_gtin_with_no_epid_at_all(self):
        row = {"epid": None, "gtin": "5060123456789", "identity_tier": None}
        assert tier_for(row) == "catalogued"

    def test_gtin_beats_epid_for_the_tier_even_when_both_are_present(self):
        row = {"epid": "epid123", "gtin": "5060123456789", "identity_tier": "inferred"}
        assert tier_for(row) == "catalogued"

    def test_inferred_when_the_transport_marked_the_epid_as_eBays_own_match(self):
        row = {"epid": "epid999", "gtin": None, "identity_tier": "inferred"}
        assert tier_for(row) == "inferred"

    def test_cluster_when_no_catalogue_match_but_title_and_leaf_exist(self):
        row = {"epid": None, "gtin": None, "identity_tier": None,
               "title": "Wireless Bluetooth Earbuds", "leaf_category_id": LEAF}
        assert tier_for(row) == "cluster"

    def test_none_when_no_leaf_even_with_a_title(self):
        row = {"epid": None, "gtin": None, "title": "Wireless Bluetooth Earbuds",
               "leaf_category_id": None}
        assert tier_for(row) == "none"

    def test_none_when_no_title_even_with_a_leaf(self):
        row = {"epid": None, "gtin": None, "title": None, "leaf_category_id": LEAF}
        assert tier_for(row) == "none"

    def test_junk_mpn_never_influences_the_tier(self):
        """mpn plays no part in tier_for at all -- a row with a junk MPN and
        one with a clean MPN, all else equal, must tier identically."""
        junky = {"epid": "epid123", "gtin": None, "identity_tier": None,
                 "mpn": "Wireless-charge-carphonr"}
        clean = {"epid": "epid123", "gtin": None, "identity_tier": None,
                 "mpn": "SM-G991B"}
        assert tier_for(junky) == tier_for(clean) == "catalogued"


class TestProductKey:
    def test_catalogued_keys_on_epid_when_no_gtin(self):
        row = {"epid": "epid123", "gtin": None}
        assert product_key(row, "catalogued") == "epid:epid123"

    def test_catalogued_keys_on_gtin_when_present(self):
        """gtin beats epid for the key, not just the tier -- two rows for
        the same product with different scraped epids (a relist can change
        it) must still land on the same key if the gtin agrees."""
        row = {"epid": "epid123", "gtin": "5060123456789"}
        assert product_key(row, "catalogued") == "gtin:5060123456789"

    def test_inferred_keys_with_a_tilde_marker(self):
        row = {"epid": "epid999"}
        assert product_key(row, "inferred") == "epid~:epid999"

    def test_catalogued_and_inferred_keys_never_collide_for_the_same_epid(self):
        row = {"epid": "epid123", "gtin": None}
        assert product_key(row, "catalogued") == "epid:epid123"
        assert product_key(row, "inferred") == "epid~:epid123"
        assert product_key(row, "catalogued") != product_key(row, "inferred")

    def test_cluster_keys_on_leaf_and_cluster_id(self):
        row = {"leaf_category_id": LEAF}
        assert product_key(row, "cluster", cluster_id=2) == f"cluster:{LEAF}:2"

    def test_cluster_without_a_cluster_id_raises(self):
        row = {"leaf_category_id": LEAF}
        with pytest.raises(ValueError):
            product_key(row, "cluster")

    def test_none_tier_has_no_key(self):
        row = {"leaf_category_id": None}
        assert product_key(row, "none") is None

    def test_mpn_never_appears_in_any_key_at_any_tier(self):
        row = {"epid": "epid1", "gtin": "5060111111111",
               "leaf_category_id": LEAF, "mpn": "Wireless-charge-carphonr"}
        for tier, kwargs in (
            ("catalogued", {}), ("inferred", {}), ("cluster", {"cluster_id": 0}),
        ):
            key = product_key(row, tier, **kwargs)
            assert key is None or "Wireless-charge-carphonr" not in key
            assert key is None or "mpn" not in key


class TestShinglesAndJaccard:
    def test_shingles_of_a_five_word_title(self):
        """"wireless bluetooth earbuds noise cancelling" -> 5 words -> 3
        overlapping 3-word shingles: positions (0,1,2), (1,2,3), (2,3,4)."""
        s = shingles("Wireless Bluetooth Earbuds Noise Cancelling")
        assert s == frozenset({
            "wireless bluetooth earbuds",
            "bluetooth earbuds noise",
            "earbuds noise cancelling",
        })

    def test_a_title_shorter_than_n_words_has_no_shingles(self):
        assert shingles("Wireless Earbuds") == frozenset()

    def test_two_empty_shingle_sets_score_zero_not_one(self):
        """Nothing in common is not identity -- two too-short titles must
        never look like a perfect match by that accident."""
        assert jaccard(shingles("A B"), shingles("C D")) == 0.0

    def test_jaccard_of_a_near_duplicate_title_pair(self):
        """A = "wireless bluetooth earbuds noise cancelling" (3 shingles,
        see above). B is A plus one trailing word, "new":
        "wireless bluetooth earbuds noise cancelling new" -> 6 words -> 4
        shingles: A's three plus "noise cancelling new".
        Intersection = A's three = 3. Union = 4 (B is a superset of A).
        Jaccard = 3 / 4 = 0.75."""
        a = shingles("Wireless Bluetooth Earbuds Noise Cancelling")
        b = shingles("Wireless Bluetooth Earbuds Noise Cancelling New")
        assert jaccard(a, b) == pytest.approx(0.75)

    def test_jaccard_of_a_partially_overlapping_but_different_title_pair(self):
        """A's shingles as above (3). F = "refurbished wireless bluetooth
        earbuds case" -> 5 words -> 3 shingles: "refurbished wireless
        bluetooth", "wireless bluetooth earbuds", "bluetooth earbuds case".
        Intersection = {"wireless bluetooth earbuds"} = 1.
        Union = 3 + 3 - 1 = 5. Jaccard = 1 / 5 = 0.2."""
        a = shingles("Wireless Bluetooth Earbuds Noise Cancelling")
        f = shingles("Refurbished Wireless Bluetooth Earbuds Case")
        assert jaccard(a, f) == pytest.approx(0.2)


class TestCluster:
    TITLE_A = "Wireless Bluetooth Earbuds Noise Cancelling"
    TITLE_B = "Wireless Bluetooth Earbuds Noise Cancelling New"
    TITLE_F = "Refurbished Wireless Bluetooth Earbuds Case"

    def _row(self, title, leaf=LEAF):
        return {"title": title, "leaf_category_id": leaf}

    def test_titles_at_jaccard_0_75_cluster_together(self):
        """A and B score 0.75 (see TestShinglesAndJaccard) -- above
        JACCARD_MIN (0.6) -- so both rows must get the same cluster id."""
        rows = [self._row(self.TITLE_A), self._row(self.TITLE_B)]
        ids = cluster(rows)
        assert ids == [0, 0]

    def test_titles_at_jaccard_0_2_do_not_cluster(self):
        """A and F score 0.2 (see TestShinglesAndJaccard) -- below 0.6 --
        so they must get different cluster ids."""
        rows = [self._row(self.TITLE_A), self._row(self.TITLE_F)]
        ids = cluster(rows)
        assert ids == [0, 1]

    def test_three_rows_two_similar_one_different(self):
        rows = [self._row(self.TITLE_A), self._row(self.TITLE_B), self._row(self.TITLE_F)]
        assert cluster(rows) == [0, 0, 1]

    def test_cluster_ids_are_assigned_in_order_of_first_appearance(self):
        rows = [self._row(self.TITLE_F), self._row(self.TITLE_A), self._row(self.TITLE_B)]
        # F starts cluster 0 (nothing to match yet); A doesn't match F
        # (0.2 < 0.6) so it starts cluster 1; B matches A (cluster 1).
        assert cluster(rows) == [0, 1, 1]

    def test_cluster_raises_when_rows_span_two_leaf_categories(self):
        rows = [self._row(self.TITLE_A, leaf="111"), self._row(self.TITLE_B, leaf="222")]
        with pytest.raises(ValueError):
            cluster(rows)

    def test_a_row_with_no_leaf_at_all_does_not_trip_the_span_guard(self):
        rows = [self._row(self.TITLE_A, leaf=LEAF), self._row(self.TITLE_B, leaf=None)]
        assert cluster(rows) == [0, 0]
