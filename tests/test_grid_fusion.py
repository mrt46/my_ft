"""Unit tests for custom_modules.grid_fusion."""

import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from custom_modules.grid_fusion import GridFusion, FusedGrid


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def grid_fusion(tmp_path):
    """Return a GridFusion with mocked settings."""
    settings = tmp_path / "config" / "settings.yaml"
    settings.parent.mkdir()
    settings.write_text(
        "bot:\n"
        "  dry_run: true\n"
    )

    base_grid_file = tmp_path / "base_grid.json"
    sentiment_file = tmp_path / "sentiment_scores.json"
    final_grid_file = tmp_path / "final_grid.json"

    with patch("custom_modules.grid_fusion.Path") as mock_path:
        mock_path.return_value.__truediv__.return_value = settings
        mock_path.return_value.parent.parent.__truediv__.return_value = settings

        fusion = GridFusion.__new__(GridFusion)
        fusion._dry_run = True
        fusion.MAX_SHIFT = 0.03
        fusion.NEUTRAL_THRESHOLD = 0.3
        fusion.BASE_GRID_FILE = base_grid_file
        fusion.SENTIMENT_FILE = sentiment_file
        fusion.FINAL_GRID_FILE = final_grid_file

        return fusion


# ---------------------------------------------------------------------------
# Grid loading
# ---------------------------------------------------------------------------

class TestGridLoading:
    def test_loads_base_grids(self, grid_fusion):
        """Should load base grids from file."""
        base_data = {
            "BTC/USDC": {
                "pair": "BTC/USDC",
                "upper_bound": 70000.0,
                "lower_bound": 60000.0,
                "levels": [60000.0, 62500.0, 65000.0, 67500.0, 70000.0],
                "level_details": [],
                "spacing": "fibonacci",
                "position_size": 10.0,
                "timestamp": time.time(),
            }
        }
        grid_fusion.BASE_GRID_FILE.write_text(json.dumps(base_data))

        result = grid_fusion._load_base_grids()

        assert "BTC/USDC" in result
        assert result["BTC/USDC"]["upper_bound"] == 70000.0

    def test_handles_missing_base_grid(self, grid_fusion):
        """Should return empty dict when base_grid.json missing."""
        result = grid_fusion._load_base_grids()

        assert result == {}

    def test_loads_sentiments(self, grid_fusion):
        """Should load sentiment scores from file."""
        sentiment_data = {
            "BTC": {
                "coin": "BTC",
                "sentiment": 0.5,
                "confidence": 0.8,
                "agreement": 0.9,
                "individual_scores": {},
                "usable": True,
                "timestamp": time.time(),
            }
        }
        grid_fusion.SENTIMENT_FILE.write_text(json.dumps(sentiment_data))

        result = grid_fusion._load_sentiments()

        assert "BTC" in result
        assert result["BTC"]["sentiment"] == 0.5

    def test_handles_missing_sentiment_file(self, grid_fusion):
        """Should return empty dict when sentiment file missing."""
        result = grid_fusion._load_sentiments()

        assert result == {}


# ---------------------------------------------------------------------------
# Fusion logic
# ---------------------------------------------------------------------------

class TestFusionLogic:
    def test_no_shift_when_sentiment_unusable(self, grid_fusion):
        """Should not shift levels when sentiment is unusable."""
        grid = {
            "pair": "BTC/USDC",
            "upper_bound": 70000.0,
            "lower_bound": 60000.0,
            "levels": [60000.0, 65000.0, 70000.0],
            "position_size": 10.0,
            "spacing": "fibonacci",
            "timestamp": time.time(),
        }
        sentiment = {
            "sentiment": 0.5,
            "usable": False,  # Unusable
        }

        result = grid_fusion._fuse("BTC/USDC", grid, sentiment)

        assert result["sentiment_shift_pct"] == 0.0
        assert result["levels"] == [60000.0, 65000.0, 70000.0]

    def test_no_shift_when_sentiment_neutral(self, grid_fusion):
        """Should have minimal shift when sentiment is neutral."""
        grid = {
            "pair": "BTC/USDC",
            "upper_bound": 70000.0,
            "lower_bound": 60000.0,
            "levels": [60000.0, 65000.0, 70000.0],
            "position_size": 10.0,
            "spacing": "fibonacci",
            "timestamp": time.time(),
        }
        sentiment = {
            "sentiment": 0.1,  # Neutral (within -0.3 to +0.3)
            "usable": True,
        }

        result = grid_fusion._fuse("BTC/USDC", grid, sentiment)

        # Small dither, less than 0.5%
        assert abs(result["sentiment_shift_pct"]) < 0.5

    def test_shifts_up_on_bullish_sentiment(self, grid_fusion):
        """Should shift levels up on bullish sentiment."""
        grid = {
            "pair": "BTC/USDC",
            "upper_bound": 70000.0,
            "lower_bound": 60000.0,
            "levels": [60000.0, 65000.0, 70000.0],
            "position_size": 10.0,
            "spacing": "fibonacci",
            "timestamp": time.time(),
        }
        sentiment = {
            "sentiment": 0.8,  # Bullish
            "usable": True,
        }

        result = grid_fusion._fuse("BTC/USDC", grid, sentiment)

        assert result["sentiment_shift_pct"] > 0  # Positive shift
        assert result["levels"][0] > 60000.0  # Shifted up
        assert result["levels"][-1] > 70000.0

    def test_shifts_down_on_bearish_sentiment(self, grid_fusion):
        """Should shift levels down on bearish sentiment."""
        grid = {
            "pair": "BTC/USDC",
            "upper_bound": 70000.0,
            "lower_bound": 60000.0,
            "levels": [60000.0, 65000.0, 70000.0],
            "position_size": 10.0,
            "spacing": "fibonacci",
            "timestamp": time.time(),
        }
        sentiment = {
            "sentiment": -0.8,  # Bearish
            "usable": True,
        }

        result = grid_fusion._fuse("BTC/USDC", grid, sentiment)

        assert result["sentiment_shift_pct"] < 0  # Negative shift
        assert result["levels"][0] < 60000.0  # Shifted down
        assert result["levels"][-1] < 70000.0

    def test_max_shift_is_capped(self, grid_fusion):
        """Maximum shift should be capped at ±3%."""
        grid = {
            "pair": "BTC/USDC",
            "upper_bound": 70000.0,
            "lower_bound": 60000.0,
            "levels": [65000.0],
            "position_size": 10.0,
            "spacing": "fibonacci",
            "timestamp": time.time(),
        }
        sentiment = {
            "sentiment": 1.0,  # Maximum bullish
            "usable": True,
        }

        result = grid_fusion._fuse("BTC/USDC", grid, sentiment)

        # Max shift is 3%
        assert result["sentiment_shift_pct"] <= 3.0
        assert result["levels"][0] == pytest.approx(66950.0, 0.01)  # 65000 * 1.03

    def test_preserves_position_size(self, grid_fusion):
        """Should preserve position_size from base grid."""
        grid = {
            "pair": "BTC/USDC",
            "upper_bound": 70000.0,
            "lower_bound": 60000.0,
            "levels": [60000.0, 65000.0, 70000.0],
            "position_size": 15.5,
            "spacing": "fibonacci",
            "timestamp": time.time(),
        }
        sentiment = {"sentiment": 0.0, "usable": True}

        result = grid_fusion._fuse("BTC/USDC", grid, sentiment)

        assert result["position_size"] == 15.5

    def test_updates_bounds_with_shift(self, grid_fusion):
        """Should update upper and lower bounds with shift."""
        grid = {
            "pair": "BTC/USDC",
            "upper_bound": 70000.0,
            "lower_bound": 60000.0,
            "levels": [60000.0, 65000.0, 70000.0],
            "position_size": 10.0,
            "spacing": "fibonacci",
            "timestamp": time.time(),
        }
        sentiment = {
            "sentiment": 0.5,
            "usable": True,
        }

        result = grid_fusion._fuse("BTC/USDC", grid, sentiment)

        # Bounds should be shifted
        assert result["upper_bound"] > 70000.0
        assert result["lower_bound"] > 60000.0


# ---------------------------------------------------------------------------
# Full run
# ---------------------------------------------------------------------------

class TestFullRun:
    def test_run_processes_all_pairs(self, grid_fusion):
        """Should process all pairs from base_grid.json."""
        base_data = {
            "BTC/USDC": {
                "pair": "BTC/USDC",
                "upper_bound": 70000.0,
                "lower_bound": 60000.0,
                "levels": [60000.0, 65000.0, 70000.0],
                "level_details": [],
                "spacing": "fibonacci",
                "position_size": 10.0,
                "timestamp": time.time(),
            },
            "ETH/USDC": {
                "pair": "ETH/USDC",
                "upper_bound": 4000.0,
                "lower_bound": 3500.0,
                "levels": [3500.0, 3750.0, 4000.0],
                "level_details": [],
                "spacing": "fibonacci",
                "position_size": 8.0,
                "timestamp": time.time(),
            },
        }
        grid_fusion.BASE_GRID_FILE.write_text(json.dumps(base_data))

        sentiment_data = {
            "BTC": {"sentiment": 0.5, "usable": True},
            "ETH": {"sentiment": -0.3, "usable": True},
        }
        grid_fusion.SENTIMENT_FILE.write_text(json.dumps(sentiment_data))

        result = grid_fusion.run()

        assert len(result) == 2
        assert "BTC/USDC" in result
        assert "ETH/USDC" in result

    def test_saves_final_grid(self, grid_fusion):
        """Should save final grid to file."""
        base_data = {
            "BTC/USDC": {
                "pair": "BTC/USDC",
                "upper_bound": 70000.0,
                "lower_bound": 60000.0,
                "levels": [60000.0, 65000.0, 70000.0],
                "level_details": [],
                "spacing": "fibonacci",
                "position_size": 10.0,
                "timestamp": time.time(),
            },
        }
        grid_fusion.BASE_GRID_FILE.write_text(json.dumps(base_data))
        grid_fusion.SENTIMENT_FILE.write_text(json.dumps({}))

        grid_fusion.run()

        assert grid_fusion.FINAL_GRID_FILE.exists()
        saved = json.loads(grid_fusion.FINAL_GRID_FILE.read_text())
        assert "BTC/USDC" in saved

    def test_fuse_pair_method(self, grid_fusion):
        """fuse_pair should be public wrapper for _fuse."""
        grid = {
            "pair": "BTC/USDC",
            "upper_bound": 70000.0,
            "lower_bound": 60000.0,
            "levels": [60000.0, 65000.0, 70000.0],
            "position_size": 10.0,
            "spacing": "fibonacci",
            "timestamp": time.time(),
        }
        sentiment = {"sentiment": 0.5, "usable": True}

        result = grid_fusion.fuse_pair("BTC/USDC", grid, sentiment)

        assert result["pair"] == "BTC/USDC"
        assert result["sentiment_applied"] is True
        assert result["sentiment_score"] == 0.5
