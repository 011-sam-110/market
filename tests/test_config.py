from phoneloop import config


def test_defaults_match_spec():
    cfg = config.load(env={})
    assert cfg.POLL_INTERVAL_S == 60
    assert cfg.PRICE_WINDOW_DAYS == 30
    assert cfg.MIN_BUCKET_SAMPLES == 8
    assert cfg.BAND_BUY_HIGH == -0.15
    assert cfg.BAND_BUY_LOW == -0.45
    assert cfg.BAND_BAIT == -0.60
    assert cfg.ALERTS_PER_BUCKET_HOUR == 3
    assert cfg.CATALOG_ID == 3565


def test_env_overrides_defaults():
    cfg = config.load(env={"PHONELOOP_POLL_INTERVAL_S": "15"})
    assert cfg.POLL_INTERVAL_S == 15


def test_bands_are_ordered_correctly():
    """bait < buy_low < buy_high < 0 — a mis-ordered band silently disables the fraud floor."""
    cfg = config.load(env={})
    assert cfg.BAND_BAIT < cfg.BAND_BUY_LOW < cfg.BAND_BUY_HIGH < 0


def test_inverted_bands_refuse_to_construct():
    """Proven to fire: swapping BAIT and BUY_HIGH would silently disable the
    fraud floor, so Config refuses to exist rather than run unguarded."""
    import pytest
    with pytest.raises(ValueError, match="bands out of order"):
        config.load(env={"PHONELOOP_BAND_BAIT": "-0.10", "PHONELOOP_BAND_BUY_HIGH": "-0.60"})
