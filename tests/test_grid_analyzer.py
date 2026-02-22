"""Unit tests for custom_modules.grid_analyzer."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

from custom_modules.grid_analyzer import GridAnalyzer


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ohlcv(n: int = 300, base_price: float = 1000.0) -> list[list]:
    """Generate synthetic OHLCV data around a base price."""
    import time as _time
    ts = int(_time.time() * 1000) - n * 60_000
    rows = []
    price = base_price
    rng = np.random.default_rng(42)
    for i in range(n):
        o = price
        h = price * (1 + rng.uniform(0, 0.01))
        l = price * (1 - rng.uniform(0, 0.01))
        c = price * (1 + rng.uniform(-0.005, 0.005))
        v = rng.uniform(100, 1000)
        rows.append([ts + i * 60_000, o, h, l, c, v])
        price = c
    return rows


def _make_df(n: int = 300, base_price: float = 1000.0) -> pd.DataFrame:
    raw = _make_ohlcv(n, base_price)
    df = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    df.set_index("timestamp", inplace=True)
    return df.astype(float)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def analyzer(tmp_path):
    settings = tmp_path / "config" / "settings.yaml"
    settings.parent.mkdir()
    settings.write_text(
        "grid:\n"
        "  sr_lookback_hours: 72\n"
        "  sr_merge_threshold_pct: 0.3\n"
        "  sr_price_bin_size: 10\n"
        "  sr_wick_multiplier: 2.0\n"
    )
    coins = tmp_path / "config" / "coins.yaml"
    coins.write_text(
        "all_grid_coins:\n  - BTC/USDC\n"
        "tiers:\n"
        "  tier_1:\n"
        "    allocation_pct: 15\n"
        "    grid_levels: 10\n"
        "    coins:\n      - BTC/USDC\n"
    )

    mock_exchange = MagicMock()
    mock_exchange.fetch_ohlcv.return_value = _make_ohlcv(4320, 50000)

    a = GridAnalyzer.__new__(GridAnalyzer)
    a._exchange = mock_exchange
    a._lookback_hours = 72
    a._merge_threshold = 0.003
    a._price_bin = 10
    a._wick_multiplier = 2.0
    a._coins_cfg = {
        "all_grid_coins": ["BTC/USDC"],
        "tiers": {
            "tier_1": {
                "allocation_pct": 15,
                "grid_levels": 10,
                "coins": ["BTC/USDC"],
            }
        },
    }
    a.BASE_GRID_FILE = tmp_path / "base_grid.json"
    return a


# ---------------------------------------------------------------------------
# _to_dataframe
# ---------------------------------------------------------------------------

class TestToDataFrame:
    def test_returns_dataframe(self, analyzer):
        df = analyzer._to_dataframe(_make_ohlcv(50))
        assert isinstance(df, pd.DataFrame)
        assert list(df.columns) == ["open", "high", "low", "close", "volume"]
        assert len(df) == 50

    def test_index_is_datetime(self, analyzer):
        df = analyzer._to_dataframe(_make_ohlcv(10))
        assert pd.api.types.is_datetime64_any_dtype(df.index)


# ---------------------------------------------------------------------------
# S/R methods
# ---------------------------------------------------------------------------

class TestSupportResistance:
    def test_returns_list(self, analyzer):
        df = _make_df(200, 1000)
        levels = analyzer.calculate_support_resistance(df)
        assert isinstance(levels, list)

    def test_levels_within_price_range(self, analyzer):
        df = _make_df(200, 1000)
        levels = analyzer.calculate_support_resistance(df)
        low = df["low"].min()
        high = df["high"].max()
        for lvl in levels:
            assert low <= lvl <= high


class TestVolumePoc:
    def test_returns_list(self, analyzer):
        df = _make_df(200, 500)
        levels = analyzer._volume_poc(df)
        assert isinstance(levels, list)
        assert len(levels) > 0


class TestRejectionWicks:
    def test_returns_list(self, analyzer):
        df = _make_df(200, 500)
        levels = analyzer._rejection_wicks(df)
        assert isinstance(levels, list)


class TestFibonacci:
    def test_returns_7_levels(self, analyzer):
        df = _make_df(50, 1000)
        levels = analyzer._fibonacci_levels(df)
        assert len(levels) == 7

    def test_levels_ordered(self, analyzer):
        df = _make_df(50, 1000)
        levels = sorted(analyzer._fibonacci_levels(df))
        assert levels == sorted(levels)


# ---------------------------------------------------------------------------
# Merge helpers
# ---------------------------------------------------------------------------

class TestMergeCloseLevels:
    def test_merges_close_levels(self, analyzer):
        analyzer._merge_threshold = 0.01  # 1%
        levels = [100.0, 100.5, 101.0, 200.0]
        merged = analyzer._merge_close_levels(levels)
        assert len(merged) == 2

    def test_preserves_distant_levels(self, analyzer):
        levels = [100.0, 200.0, 300.0]
        merged = analyzer._merge_close_levels(levels)
        assert len(merged) == 3

    def test_empty_input(self, analyzer):
        assert analyzer._merge_close_levels([]) == []


class TestMergeLevels:
    def test_combines_sources(self, analyzer):
        result = analyzer._merge_levels([
            ([1000.0, 1001.0], "sr"),
            ([1000.5], "fib"),
        ])
        assert isinstance(result, list)
        assert all("sources" in lvl for lvl in result)

    def test_strength_increases_on_overlap(self, analyzer):
        analyzer._merge_threshold = 0.01
        result = analyzer._merge_levels([
            ([1000.0], "sr"),
            ([1000.3], "fib"),
            ([1000.6], "poc"),
        ])
        # All three within 1% — should merge to 1 level with strength 3
        assert len(result) == 1
        assert result[0]["strength"] == 3


# ---------------------------------------------------------------------------
# Position size
# ---------------------------------------------------------------------------

class TestPositionSize:
    def test_tier1_position_size(self, analyzer):
        size = analyzer._get_position_size("BTC/USDC")
        assert size > 0

    def test_unknown_pair_returns_default(self, analyzer):
        size = analyzer._get_position_size("UNKNOWN/USDC")
        assert size == 10.0


# ---------------------------------------------------------------------------
# analyze (integration-style)
# ---------------------------------------------------------------------------

class TestAnalyze:
    def test_returns_grid_config(self, analyzer):
        config = analyzer.analyze("BTC/USDC")
        assert config["pair"] == "BTC/USDC"
        assert isinstance(config["levels"], list)
        assert len(config["levels"]) > 0
        assert config["upper_bound"] > config["lower_bound"]

    def test_saves_to_file(self, analyzer):
        analyzer.analyze("BTC/USDC")
        assert analyzer.BASE_GRID_FILE.exists()
        data = json.loads(analyzer.BASE_GRID_FILE.read_text())
        assert "BTC/USDC" in data
