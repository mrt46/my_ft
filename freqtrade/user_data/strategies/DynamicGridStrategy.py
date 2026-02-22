"""DynamicGridStrategy — Freqtrade grid strategy using AI-fused grid levels.

Reads ``data/final_grid.json`` produced by GridFusion and executes buy/sell
orders at each calculated support/resistance level.

Grid logic:
    - Entry:  Price drops to a grid support level (within 0.5%)
    - Exit:   Price rises to the next grid level above avg entry (custom_exit)
    - DCA:    Position is added at each lower grid level (adjust_trade_position)
    - Backup: minimal_roi (3%) and stoploss (-12%) as safety net

Profit target: NET %3 per trade (minimal_roi: {"0": 0.03})
Telegram:      dp.send_msg() — requires allow_custom_messages: true in config.json

File location assumed: freqtrade/user_data/strategies/DynamicGridStrategy.py
Grid file:             my_ft/data/final_grid.json  (4 directories up)
"""

import json
import logging
import time
from datetime import datetime, timezone
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

# Minimum profit before grid TP exit is considered (1% — aligned with 3% daily target)
MIN_PROFIT_FOR_EXIT = 0.01


class DynamicGridStrategy(IStrategy):
    """Grid trading strategy driven by AI-fused support/resistance levels.

    Grid levels are calculated externally by:
        GridAnalyzer → SentimentAnalyzer → GridFusion → final_grid.json

    This strategy reads those levels every 5 minutes and:
        1. Buys when price drops to a grid support level
        2. Adds to the position at each lower grid level (DCA)
        3. Exits when price rises to the next level above avg entry

    Profit target: %3 net per trade (minimal_roi safety net + custom_exit grid TP)
    Telegram:      Startup grid summary, TP exits, DCA entries via dp.send_msg()
    """

    INTERFACE_VERSION = 3

    # Allow adding to position at lower grid levels (DCA mechanic)
    position_adjustment_enable = True
    max_entry_position_adjustment = 8  # max 9 total entries per pair

    # Target ROI — NET %3 per trade (daily_profit_target_pct: 3.0 in settings.yaml)
    # custom_exit handles grid take-profits; minimal_roi is the safety net
    minimal_roi = {
        "0": 0.03,      # %3 — günlük kar hedefi (net)
        "1440": 0.02,   # %2 — 1 günden sonra
        "2880": 0.01,   # %1 — 2 günden sonra
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

    # Track which pairs we've already sent grid notifications for (avoid spam)
    _grid_notified: set = set()

    # Track startup notification (sent once per bot start)
    _startup_notified: bool = False

    # -------------------------------------------------------------------------
    # Telegram helpers
    # -------------------------------------------------------------------------

    def _dp_send(self, msg: str, always_send: bool = True) -> None:
        """Send a message via Freqtrade's dp.send_msg() (requires allow_custom_messages: true).

        Args:
            msg: Plain-text message (no HTML — dp.send_msg uses plain text).
            always_send: If True, bypasses dedup cache. Default True for trade events.
        """
        try:
            if hasattr(self, "dp") and self.dp:
                self.dp.send_msg(msg, always_send=always_send)
            else:
                logger.warning("[TG] DataProvider not available, cannot send message")
        except Exception as exc:
            logger.error("[TG] dp.send_msg failed: %s", exc)

    def _send_grid_telegram(self, pair: str, levels: list[float], current_rate: float) -> None:
        """Send grid levels for a pair to Telegram on first load.

        Called once per pair when grid levels are first loaded or refreshed.
        Requires allow_custom_messages: true in config.json telegram section.
        """
        try:
            grid_data = self._cache.get(pair, {})
            position_size = grid_data.get("position_size", "?")
            upper = grid_data.get("upper_bound", levels[-1] if levels else 0)
            lower = grid_data.get("lower_bound", levels[0] if levels else 0)
            sentiment_score = grid_data.get("sentiment_score", None)
            spacing = grid_data.get("spacing", "?")

            # Find nearest support and resistance relative to current price
            below = [lv for lv in levels if lv <= current_rate]
            above = [lv for lv in levels if lv > current_rate]
            nearest_support = max(below) if below else None
            nearest_resist = min(above) if above else None

            # Build levels list (mark current price position with arrow)
            levels_str = ""
            for lvl in sorted(levels):
                if nearest_support and abs(lvl - nearest_support) < 0.0001:
                    levels_str += f"  >> ${lvl:,.4f}  <- destek\n"
                elif nearest_resist and abs(lvl - nearest_resist) < 0.0001:
                    levels_str += f"  >> ${lvl:,.4f}  <- hedef\n"
                else:
                    levels_str += f"  . ${lvl:,.4f}\n"

            sentiment_line = ""
            if sentiment_score is not None:
                s_emoji = "+" if sentiment_score > 0.1 else ("-" if sentiment_score < -0.1 else "~")
                sentiment_line = f"\nSentiment [{s_emoji}]: {sentiment_score:+.2f}"

            support_line = f"Destek: ${nearest_support:,.4f}\n" if nearest_support else ""
            resist_line = f"Hedef:  ${nearest_resist:,.4f}\n" if nearest_resist else ""

            msg = (
                f"GRID SEVIYELERI -- {pair}\n"
                f"----------------------------\n"
                f"Pozisyon: {position_size} USDC\n"
                f"Ust sinir: ${upper:,.4f}\n"
                f"Alt sinir: ${lower:,.4f}\n"
                f"Fiyat: ${current_rate:,.4f}\n"
                f"{support_line}"
                f"{resist_line}"
                f"Aralik: {spacing}{sentiment_line}\n\n"
                f"Seviyeler ({len(levels)} adet):\n"
                f"{levels_str}"
                f"----------------------------\n"
                f"{datetime.now(timezone.utc).strftime('%H:%M UTC')}"
            )

            self._dp_send(msg, always_send=True)
            logger.info("[GRID NOTIFY] Sent grid levels for %s to Telegram", pair)
        except Exception as exc:
            logger.error("[GRID NOTIFY] Failed to send grid levels for %s: %s", pair, exc)

    def _send_startup_summary(self) -> None:
        """Send a one-time startup summary of all loaded grid pairs."""
        try:
            if not self._cache:
                return
            pairs = list(self._cache.keys())
            lines = [
                f"Bot baslatildi -- DynamicGridStrategy",
                f"----------------------------",
                f"Kar hedefi: %3 net (minimal_roi)",
                f"Stop-loss: %12",
                f"Timeframe: 5m",
                f"Grid dosyasi: final_grid.json",
                f"",
                f"Yuklenen coinler ({len(pairs)}):",
            ]
            for pair in pairs:
                gd = self._cache.get(pair, {})
                n = len(gd.get("levels", []))
                ps = gd.get("position_size", "?")
                lines.append(f"  {pair}: {n} seviye, {ps} USDC/pozisyon")
            lines.append(f"----------------------------")
            lines.append(f"{datetime.now(timezone.utc).strftime('%H:%M UTC')}")
            self._dp_send("\n".join(lines), always_send=True)
            logger.info("[STARTUP] Startup summary sent to Telegram")
        except Exception as exc:
            logger.error("[STARTUP] Failed to send startup summary: %s", exc)

    # -------------------------------------------------------------------------
    # Cache management
    # -------------------------------------------------------------------------

    def _refresh_cache(self) -> None:
        """Reload final_grid.json if cache is stale."""
        now = time.time()
        if now - self._cache_ts < self._CACHE_TTL:
            return
        try:
            if FINAL_GRID_FILE.exists():
                old_pairs = set(self._cache.keys())
                self._cache = json.loads(FINAL_GRID_FILE.read_text(encoding="utf-8"))
                self._cache_ts = now
                new_pairs = set(self._cache.keys())
                logger.debug("Grid cache refreshed: %d pairs", len(self._cache))
                # Reset notifications for pairs whose grid data changed
                changed = new_pairs - old_pairs
                if changed:
                    self._grid_notified -= changed
                    logger.info("Grid updated for new pairs: %s", changed)
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
    # Freqtrade lifecycle hooks
    # -------------------------------------------------------------------------

    def bot_loop_start(self, current_time: datetime, **kwargs) -> None:
        """Called at the start of each bot loop iteration (every ~5s).

        Used to:
        - Send one-time startup summary to Telegram
        - Force cache refresh on first run
        """
        if not self._startup_notified:
            # Force cache load on first run
            self._cache_ts = 0.0
            self._refresh_cache()
            if self._cache:
                self._send_startup_summary()
                self._startup_notified = True

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

        # Send grid levels to Telegram once per pair (on first load)
        if pair not in self._grid_notified and len(dataframe) > 0:
            current_rate = float(dataframe["close"].iloc[-1])
            self._send_grid_telegram(pair, levels, current_rate)
            self._grid_notified.add(pair)

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
    # Grid take-profit exit — %3 net kar hedefi
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

        Profit target: NET %3 (MIN_PROFIT_FOR_EXIT = 0.01 as noise filter).
        minimal_roi {"0": 0.03} acts as safety net if grid TP is not triggered.
        Sends detailed Telegram notification on TP exit.
        """
        # Noise filter: ignore tiny moves (aligned with %3 daily target)
        if current_profit < MIN_PROFIT_FOR_EXIT:
            return None

        levels = self._levels(pair)
        if not levels:
            return None

        avg = trade.open_rate
        # Find all grid levels meaningfully above the average entry
        above = [lv for lv in levels if lv > avg * 1.001]
        if not above:
            return None

        target = min(above)  # nearest level above entry = take-profit target
        if current_rate >= target * (1.0 - PROXIMITY_PCT / 2.0):
            profit_pct = current_profit * 100
            hold_hours = (
                (current_time - trade.open_date_utc).total_seconds() / 3600
                if hasattr(trade, "open_date_utc") and trade.open_date_utc
                else 0.0
            )
            profit_usdc = current_profit * trade.stake_amount

            logger.info(
                "[GRID TP] %s: rate=%.4f target=%.4f profit=+%.2f%% hold=%.1fh",
                pair, current_rate, target, profit_pct, hold_hours,
            )

            # Telegram TP notification
            self._dp_send(
                f"GRID TP -- {pair}\n"
                f"----------------------------\n"
                f"Kar: +{profit_pct:.2f}% (+{profit_usdc:.2f} USDC)\n"
                f"Fiyat: ${current_rate:,.4f}\n"
                f"Hedef seviye: ${target:,.4f}\n"
                f"Hold: {hold_hours:.1f} saat\n"
                f"Pozisyon: {trade.stake_amount:.2f} USDC\n"
                f"----------------------------\n"
                f"{datetime.now(timezone.utc).strftime('%H:%M UTC')}",
                always_send=True,
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
        Sends Telegram notification on each DCA trigger.
        """
        levels = self._levels(trade.pair)
        if not levels:
            return None

        # Grid levels strictly below average entry, sorted highest-first
        below = sorted(
            [lv for lv in levels if lv < trade.open_rate * 0.999],
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

        # Telegram DCA notification
        self._dp_send(
            f"GRID DCA #{dca_done + 1} -- {trade.pair}\n"
            f"----------------------------\n"
            f"Eklenen: {stake:.2f} USDC\n"
            f"Fiyat: ${current_rate:,.4f}\n"
            f"Seviye: ${next_level:,.4f}\n"
            f"Mevcut kar: {current_profit * 100:+.2f}%\n"
            f"Toplam giris: {trade.nr_of_successful_entries + 1}\n"
            f"----------------------------\n"
            f"{datetime.now(timezone.utc).strftime('%H:%M UTC')}",
            always_send=True,
        )
        return stake

    # -------------------------------------------------------------------------
    # Custom stake amount (use position_size from grid config)
    # -------------------------------------------------------------------------

    def custom_stake_amount(
        self,
        current_time,
        current_rate: float,
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
