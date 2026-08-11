from stock_db.performance import _resolve_summary_strategy


def test_bare_reversal_name_resolves_to_v12_storage_name():
    assert (
        _resolve_summary_strategy("reversal_reclaim")
        == "v12_reversal_reclaim"
    )


def test_prefixed_reversal_name_is_unchanged():
    assert (
        _resolve_summary_strategy("v12_reversal_reclaim")
        == "v12_reversal_reclaim"
    )


def test_legacy_v11_strategy_name_is_unchanged():
    assert _resolve_summary_strategy("early_stage") == "early_stage"


def test_strategy_normalisation_is_case_and_space_insensitive():
    assert (
        _resolve_summary_strategy("  Reversal_Reclaim  ")
        == "v12_reversal_reclaim"
    )


def test_none_and_blank_mean_all_strategies():
    assert _resolve_summary_strategy(None) is None
    assert _resolve_summary_strategy("  ") is None
