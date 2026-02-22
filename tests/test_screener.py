"""Unit tests for custom_modules.screener."""

import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

from custom_modules.screener import Screener, ScreenerCandidate


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def screener(tmp_path):
    settings = tmp_path / "config" / "settings.yaml"
    settings.parent.mkdir()
    settings.write_text(
        "screener:\n"
        "  volume_min_24h: 5000000\n"
        "  rsi_4h_threshold: 35\n"
        "  rsi_1d_threshold: 30\n"
        "  ema200_period: 200\n"
        "  min_score: 40\n"
        "  top_n: 5\n"
    )

    mock_exchange = MagicMock()
    mock_exchange.exchange = MagicMock()

    s = Screener.__new__(Screener)
    s._exchange = mock_exchange
    s._volume_min = 5_000_000
    s._rsi_4h_thr = 35
    s._rsi_1d_thr = 30
    s._ema_period = 200
    s._min_score = 40
    s._top_n = 5
    s.QUEUE_FILE = tmp_path / "screener_queue.json"
    return s


def _make_ohlcv_data(n: int, base: float = 1.0, trend: float = 0.0) -> list[list]:
    """Synthetic descending OHLCV so EMA200 > price."""
    ts = int(time.time() * 1000)
    rows = []
    price = base
    rng = np.random.default_rng(1)
    for i in range(n):
        o = price
        h = price * 1.005
        l = price * 0.995
        c = price * (1 + trend + rng.uniform(-0.002, 0.002))
        v = rng.uniform(1000, 5000)
        rows.append([ts - (n - i) * 3_600_000, o, h, l, c, v])
        price = c
    return rows


# ---------------------------------------------------------------------------
# calculate_opportunity_score
# ---------------------------------------------------------------------------

class TestOpportunityScore:
    def test_perfect_score(self, screener):
        # RSI4h=20 (+30), RSI1d=22 (+40), dist=6% (+25), vol=60M (+20) = 115
        score = screener.calculate_opportunity_score(20, 22, 6, 60_000_000)
        assert score == 115

    def test_minimal_score(self, screener):
        # Just qualifies: RSI4h=34, RSI1d=29, dist=20, vol=6M
        score = screener.calculate_opportunity_score(34, 29, 20, 6_000_000)
        assert score > 0

    def test_zero_score(self, screener):
        # No oversold conditions and no volume
        score = screener.calculate_opportunity_score(50, 50, 30, 1_000_000)
        assert score == 0

    def test_rsi4h_thresholds(self, screener):
        # distance=10% (8–15 range) adds +15 pts to each result
        assert screener.calculate_opportunity_score(20, 50, 10, 0) == 45   # 30 + 15
        assert screener.calculate_opportunity_score(27, 50, 10, 0) == 35   # 20 + 15
        assert screener.calculate_opportunity_score(33, 50, 10, 0) == 25   # 10 + 15
        assert screener.calculate_opportunity_score(40, 50, 10, 0) == 15   # 0  + 15

    def test_ema_distance_sweet_spot(self, screener):
        # 3–8% is the sweet spot (25 pts)
        score_sweet = screener.calculate_opportunity_score(50, 50, 5, 0)
        score_close = screener.calculate_opportunity_score(50, 50, 1, 0)
        assert score_sweet > score_close


# ---------------------------------------------------------------------------
# calculate_screener_position_size
# ---------------------------------------------------------------------------

class TestPositionSize:
    def _candidate(self, score: int, volume: float) -> ScreenerCandidate:
        return ScreenerCandidate(
            pair="TEST/USDC", price=1.0, rsi_4h=25, rsi_1d=25,
            ema200=1.2, distance_pct=20, volume=volume,
            score=score, timestamp=time.time()
        )

    def test_excellent_high_volume(self, screener):
        c = self._candidate(85, 60_000_000)
        size = screener.calculate_screener_position_size(c, 500)
        assert size == 100.0  # 100 * 1.2 → capped at 100

    def test_medium_score(self, screener):
        c = self._candidate(50, 15_000_000)
        size = screener.calculate_screener_position_size(c, 500)
        assert 20 <= size <= 100

    def test_respects_min_20(self, screener):
        c = self._candidate(50, 15_000_000)
        size = screener.calculate_screener_position_size(c, 5)  # very low available
        assert size >= 20

    def test_respects_max_100(self, screener):
        c = self._candidate(100, 100_000_000)
        size = screener.calculate_screener_position_size(c, 1000)
        assert size <= 100

    def test_limited_by_available(self, screener):
        c = self._candidate(85, 60_000_000)
        size = screener.calculate_screener_position_size(c, 30)
        assert size <= 30


# ---------------------------------------------------------------------------
# Technical indicators
# ---------------------------------------------------------------------------

class TestRsi:
    def test_rsi_range(self, screener):
        df = pd.DataFrame({"close": [1.0 + i * 0.01 for i in range(50)]})
        df.index = pd.date_range("2024-01-01", periods=50, freq="1h")
        rsi = screener._calculate_rsi(df, period=14)
        assert 0 <= rsi <= 100

    def test_downtrend_rsi_below_50(self, screener):
        close = [100.0 - i * 0.5 for i in range(50)]
        df = pd.DataFrame({"close": close})
        df.index = pd.date_range("2024-01-01", periods=50, freq="1h")
        rsi = screener._calculate_rsi(df)
        assert rsi < 50


class TestEma:
    def test_ema_close_to_last_on_flat_series(self, screener):
        close = [100.0] * 250
        df = pd.DataFrame({"close": close})
        df.index = pd.date_range("2024-01-01", periods=250, freq="1d")
        ema = screener._calculate_ema(df, period=200)
        assert abs(ema - 100.0) < 0.01


# ---------------------------------------------------------------------------
# daily_screener (integration-style with mocked exchange)
# ---------------------------------------------------------------------------

class TestDailyScreener:
    def test_returns_list(self, screener):
        screener._get_all_usdc_pairs = MagicMock(return_value=[])
        result = screener.daily_screener()
        assert isinstance(result, list)

    def test_filters_low_volume_pairs(self, screener):
        screener._get_all_usdc_pairs = MagicMock(return_value=["LOW/USDC"])
        screener._exchange.fetch_ticker.return_value = {
            "last": 1.0, "quoteVolume": 100_000  # below 5M
        }
        result = screener.daily_screener()
        assert result == []

    def test_saves_queue(self, screener):
        screener._get_all_usdc_pairs = MagicMock(return_value=[])
        screener.daily_screener()
        assert screener.QUEUE_FILE.exists()
