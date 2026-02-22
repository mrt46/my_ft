"""DynamicGridStrategy — Freqtrade grid strategy using AI-fused grid levels.

Reads ``data/final_grid.json`` produced by GridFusion and executes buy/sell
orders at each calculated support/resistance level.

Grid logic:
    - Entry:  Price drops to a grid support level (within 0.5%)
    - Exit:   Price rises to the next grid level above avg entry (custom_exit)
    - DCA:    Position is added at each lower grid level (adjust_trade_position)
    - Backup: minimal_roi and stoploss as safety net

File location assumed: freqtrade/user_data/strategies/DynamicGridStrategy.py
Grid file:             my_ft/data/final_grid.json  (4 directories up)
"""

import json
import logging
import time
from pathlib import Path
from typing import Optional

import numpy as np
from pandas import DataFrame

from freqtrade.persistence import Trade
from freqtrade.strategy import IStrategy

logger = logging.getLogger(__name__)

# Path: strategies/ → user_data/ → freqtrade/ → my_ft/ → data/final_grid.json
FINAL_GRID_FILE = Path(__file__).parents[3] / "data" / "final_grid.json"

# How close (%) to a grid level triggers a signal
PROXIMITY_PCT = 0.005  # 0.5%


class DynamicGridStrategy(IStrategy):
    """Grid trading strategy driven by AI-fused support/resistance levels.

    Grid levels are calculated externally by:
        GridAnalyzer → SentimentAnalyzer → GridFusion → final_grid.json

    This strategy reads those levels every 5 minutes and:
        1. Buys when price drops to a grid support level
        2. Adds to the position at each lower grid level (DCA)
        3. Exits when price rises to the next level above avg entry
    """

    INTERFACE_VERSION = 3

    # Allow adding to position at lower grid levels (DCA mechanic)
    position_adjustment_enable = True
    max_entry_position_adjustment = 8  # max 9 total entries per pair

    # Backup ROI — custom_exit handles grid take-profits primarily
    minimal_roi = {
        "0": 0.20,      # 20% at any time
        "2880": 0.10,   # 10% after 2 days
        "7200": 0.05,   # 5% after 5 days
    }

    # Fallback stop-loss (below lowest expected grid level)
    stoploss = -0.12

    trailing_stop = False
    timeframe = "5m"
    process_only_new_candles = True
    can_short = False

    # -------------------------------------------------------------------------
    # Grid data cache
    # -------------------------------------------------------------------------

    _cache: dict = {}
    _cache_ts: float = 0.0
    _CACHE_TTL: float = 300.0  # refresh every 5 minutes

    def _refresh_cache(self) -> None:
        """Reload final_grid.json if cache is stale."""
        now = time.time()
        if now - self._cache_ts < self._CACHE_TTL:
            return
        try:
            if FINAL_GRID_FILE.exists():
                self._cache = json.loads(FINAL_GRID_FILE.read_text(encoding="utf-8"))
                self._cache_ts = now
                logger.debug("Grid cache refreshed: %d pairs", len(self._cache))
            else:
                logger.warning("final_grid.json not found at %s", FINAL_GRID_FILE)
        except Exception as exc:
            logger.warning("Cannot load final_grid.json: %s", exc)

    def _levels(self, pair: str) -> list[float]:
        """Return sorted grid levels for a pair, or [] if not found."""
        self._refresh_cache()
        return sorted(self._cache.get(pair, {}).get("levels", []))

    def _grid_stake(self, pair: str, fallback: float) -> float:
        """Return position_size from grid config, or fallback."""
        self._refresh_cache()
        return float(self._cache.get(pair, {}).get("position_size", fallback))

    # -------------------------------------------------------------------------
    # Indicators
    # -------------------------------------------------------------------------

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """Mark candles where price is at a grid support level."""
        pair = metadata["pair"]
        levels = self._levels(pair)

        if not levels:
            dataframe["near_support"] = 0
            dataframe["grid_support"] = np.nan
            return dataframe

        arr = np.array(levels, dtype=float)
        close = dataframe["close"].values.astype(float)

        # Find the highest grid level that is BELOW each close price
        # searchsorted returns the insertion index; arr[idx-1] = level just below
        idx = np.searchsorted(arr, close, side="right")
        below_idx = np.clip(idx - 1, 0, len(arr) - 1)
        below = np.where(idx > 0, arr[below_idx], np.nan)

        # "At support" = close is within PROXIMITY_PCT above a grid level
        # i.e., the price has dropped to the level and is hovering just above it
        at_support = (
            (~np.isnan(below))
            & (close >= below)
            & (close <= below * (1.0 + PROXIMITY_PCT))
        ).astype(int)

        dataframe["near_support"] = at_support
        dataframe["grid_support"] = below
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """Enter long when price touches a grid support level."""
        dataframe.loc[
            (dataframe["near_support"] == 1) & (dataframe["volume"] > 0),
            ["enter_long", "enter_tag"],
        ] = [1, "grid_support"]
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """Exits are handled by custom_exit; minimal_roi is the safety net."""
        dataframe["exit_long"] = 0
        return dataframe

    # -------------------------------------------------------------------------
    # Grid take-profit exit
    # -------------------------------------------------------------------------

    def custom_exit(
        self,
        pair: str,
        trade: "Trade",
        current_time,
        current_rate: float,
        current_profit: float,
        **kwargs,
    ) -> Optional[str]:
        """Exit when price reaches the next grid level above average entry.

        Waits for at least +0.5% profit to avoid noise exits.
        """
        if current_profit < 0.005:
            return None

        levels = self._levels(pair)
        if not levels:
            return None

        avg = trade.open_rate
        # Find all grid levels meaningfully above the average entry
        above = [l for l in levels if l > avg * 1.001]
        if not above:
            return None

        target = min(above)  # nearest level above entry = take-profit target
        if current_rate >= target * (1.0 - PROXIMITY_PCT / 2.0):
            logger.info(
                "[GRID TP] %s: rate=%.4f target=%.4f profit=+%.2f%%",
                pair, current_rate, target, current_profit * 100,
            )
            return "grid_tp"

        return None

    # -------------------------------------------------------------------------
    # DCA: add to position at each lower grid level
    # -------------------------------------------------------------------------

    def adjust_trade_position(
        self,
        trade: "Trade",
        current_time,
        current_rate: float,
        current_profit: float,
        min_stake: Optional[float],
        max_stake: float,
        current_entry_rate: float,
        current_exit_rate: float,
        current_entry_profit: float,
        current_exit_profit: float,
        **kwargs,
    ) -> Optional[float]:
        """Add to position when price drops to the next lower grid level.

        Each call checks whether price has reached the (N+1)-th grid level
        below the average entry, where N = number of DCA entries so far.
        """
        levels = self._levels(trade.pair)
        if not levels:
            return None

        # Grid levels strictly below average entry, sorted highest-first
        below = sorted(
            [l for l in levels if l < trade.open_rate * 0.999],
            reverse=True,
        )
        if not below:
            return None

        # How many DCA adjustments have already been made?
        # nr_of_successful_entries includes the initial entry (= 1 at start)
        dca_done = trade.nr_of_successful_entries - 1
        if dca_done >= self.max_entry_position_adjustment or dca_done >= len(below):
            return None

        next_level = below[dca_done]

        # Only trigger if price is actually AT this level
        if current_rate > next_level * (1.0 + PROXIMITY_PCT):
            return None

        stake = self._grid_stake(trade.pair, min_stake or 10.0)
        if min_stake:
            stake = max(stake, min_stake)
        stake = min(stake, max_stake)

        logger.info(
            "[GRID DCA] %s: adding %.2f USDC @ %.4f (level=%.4f, dca#%d)",
            trade.pair, stake, current_rate, next_level, dca_done + 1,
        )
        return stake

    # -------------------------------------------------------------------------
    # Custom stake amount (use position_size from grid config)
    # -------------------------------------------------------------------------

    def custom_stake_amount(
        self,
        current_time,
        current_rate: float,
        current_profit: float,
        proposed_stake: float,
        min_stake: Optional[float],
        max_stake: float,
        leverage: float,
        entry_tag: Optional[str],
        side: str,
        **kwargs,
    ) -> float:
        """Use position_size from final_grid.json for the initial entry."""
        pair = kwargs.get("pair", "")
        stake = self._grid_stake(pair, proposed_stake)
        if min_stake and stake < min_stake:
            stake = min_stake
        return min(stake, max_stake)
