"""Grid Analyzer — Support/Resistance, Fibonacci, and Volume Profile.

Calculates dynamic grid levels from 72-hour OHLCV data using four
complementary methods, then merges and ranks the resulting price zones.
Results are saved to ``data/base_grid.json`` for consumption by grid_fusion.
"""

import json
import logging
from pathlib import Path
from typing import TypedDict

import numpy as np
import pandas as pd
import yaml

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# TypedDicts
# ---------------------------------------------------------------------------

class GridLevel(TypedDict):
    """A single validated grid price level."""

    price: float
    strength: int        # 1–4, higher = more confirmation methods
    sources: list[str]   # e.g. ['sr', 'volume_poc', 'fib']


class GridConfig(TypedDict):
    """Full grid configuration for one trading pair."""

    pair: str
    upper_bound: float
    lower_bound: float
    levels: list[float]
    level_details: list[GridLevel]
    spacing: str         # 'arithmetic' | 'fibonacci'
    position_size: float
    timestamp: float


# ---------------------------------------------------------------------------
# GridAnalyzer
# ---------------------------------------------------------------------------

class GridAnalyzer:
    """Compute dynamic grid levels from raw OHLCV data.

    Algorithm (4 layers):
        1. **Price touch frequency** — count candles touching each price bin.
        2. **Volume Profile / POC** — find price where most volume traded.
        3. **Rejection wicks** — candles with wick > 2× body signal reversal.
        4. **Fibonacci retracements** — 23.6%, 38.2%, 50%, 61.8%, 78.6%.

    Then merge levels that are within ``sr_merge_threshold_pct`` of each other
    (keep the strongest), and output the final sorted list.

    Example:
        analyzer = GridAnalyzer(exchange_wrapper)
        grid = analyzer.analyze('BTC/USDC')
    """

    BASE_GRID_FILE = Path(__file__).parent.parent / "data" / "base_grid.json"

    # Stablecoins to exclude from trading
    STABLECOINS = {
        "USDT", "USDC", "BUSD", "DAI", "TUSD", "FDUSD", "USD1",
        "USDD", "UST", "USDP", "GUSD", "HUSD", "SUSD", "EURC"
    }

    def __init__(self, exchange_wrapper) -> None:
        """Initialise with a live exchange wrapper.

        Args:
            exchange_wrapper: Instance of ``ResilientExchangeWrapper``.
        """
        self._exchange = exchange_wrapper

        settings_path = Path(__file__).parent.parent / "config" / "settings.yaml"
        with open(settings_path) as fh:
            cfg = yaml.safe_load(fh)

        g = cfg.get("grid", {})
        self._lookback_hours: int = g.get("sr_lookback_hours", 72)
        self._merge_threshold: float = g.get("sr_merge_threshold_pct", 0.3) / 100
        self._price_bin_pct: float = g.get("sr_price_bin_pct", 0.5) / 100  # e.g. 0.5% of price
        self._wick_multiplier: float = g.get("sr_wick_multiplier", 2.0)

        coins_path = Path(__file__).parent.parent / "config" / "coins.yaml"
        with open(coins_path) as fh:
            self._coins_cfg = yaml.safe_load(fh)

        logger.info("GridAnalyzer initialised")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def analyze(self, pair: str, rank: int = 0) -> GridConfig:
        """Run full grid analysis pipeline for one pair with tier-based settings.

        Args:
            pair: Trading pair, e.g. ``'BTC/USDC'``.
            rank: Volume ranking (0-9), determines tier and grid levels

        Returns:
            GridConfig with sorted price levels and tier-based metadata.
        """
        logger.info(f"Grid analysis: {pair} (rank={rank})")

        # Fetch 72 h of 1-minute candles (4320 rows)
        limit = self._lookback_hours * 60
        raw = self._exchange.fetch_ohlcv(pair, "1m", limit=limit)
        df = self._to_dataframe(raw)

        # Run all four methods
        sr_levels = self._price_touch_frequency(df)
        poc_levels = self._volume_poc(df)
        wick_levels = self._rejection_wicks(df)
        fib_levels = self._fibonacci_levels(df)

        # Merge with source tracking
        all_levels = self._merge_levels(
            [
                (sr_levels, "sr"),
                (poc_levels, "volume_poc"),
                (wick_levels, "wick"),
                (fib_levels, "fib"),
            ]
        )

        current_price = float(df["close"].iloc[-1])

        # Tier-based grid levels
        if rank < 3:
            target_levels = 10  # Tier 1: Large Cap
        elif rank < 6:
            target_levels = 8   # Tier 2: Mid Cap
        else:
            target_levels = 6   # Tier 3: Small Cap

        # Adjust based on volatility (ATR)
        try:
            atr = self._calculate_atr(df)
            atr_pct = (atr / current_price) * 100
            if atr_pct > 3.0:
                target_levels = max(5, target_levels - 2)  # High vol = fewer levels
            elif atr_pct < 1.0:
                target_levels = min(15, target_levels + 2)  # Low vol = more levels
        except Exception:
            pass  # Use default tier levels

        # Generate evenly distributed levels
        lower = min(l["price"] for l in all_levels) * 0.98
        upper = max(l["price"] for l in all_levels) * 1.02
        dynamic_levels = np.linspace(lower, upper, target_levels).tolist()

        position_size = self._get_position_size(pair, rank)

        config: GridConfig = {
            "pair": pair,
            "upper_bound": round(upper, 6),
            "lower_bound": round(lower, 6),
            "levels": [round(l, 6) for l in dynamic_levels],
            "level_details": all_levels,
            "spacing": f"tier_{target_levels}levels",
            "position_size": position_size,
            "timestamp": df.index[-1].timestamp(),
        }

        self._save(pair, config)
        logger.info(f"Grid analysis done: {pair} -> {target_levels} levels (tier {rank//3 + 1})")
        return config

    def analyze_all(self) -> dict[str, GridConfig]:
        """Analyse all coins defined in ``config/coins.yaml``.

        Returns:
            Mapping of pair → GridConfig.
        """
        results: dict[str, GridConfig] = {}
        all_coins: list[str] = self._coins_cfg.get("all_grid_coins", [])
        # Filter stablecoins
        all_coins = [p for p in all_coins if p.split("/")[0] not in self.STABLECOINS]
        for pair in all_coins:
            try:
                results[pair] = self.analyze(pair)
            except Exception as exc:
                logger.error(f"Grid analysis failed for {pair}: {exc}")
        return results

    # ------------------------------------------------------------------
    # Dynamic top volume pairs
    # ------------------------------------------------------------------

    def get_top_volume_pairs(self, top_n: int = 10, min_volume_24h: float = 10_000_000) -> list[str]:
        """Get top N USDC pairs by 24h trading volume from Binance.

        Filters out stablecoins and only returns actual trading pairs.

        Args:
            top_n: Number of pairs to return (default 10)
            min_volume_24h: Minimum 24h volume in USDC (default 10M)

        Returns:
            List of pair symbols sorted by volume (descending)
        """
        try:
            markets = self._exchange.exchange.load_markets()
            usdc_pairs = [
                symbol for symbol, market in markets.items()
                if market.get("quote") == "USDC"
                and market.get("active", False)
                and market.get("spot", False)
                and market.get("base") not in self.STABLECOINS  # Exclude stablecoins
            ]

            volumes: list[tuple[str, float]] = []
            for pair in usdc_pairs:
                try:
                    ticker = self._exchange.fetch_ticker(pair)
                    vol = ticker.get("quoteVolume", 0)
                    if vol >= min_volume_24h:
                        volumes.append((pair, vol))
                except Exception:
                    continue

            volumes.sort(key=lambda x: x[1], reverse=True)
            top_pairs = [p for p, _ in volumes[:top_n]]

            logger.info(f"Top {len(top_pairs)} volume pairs (stablecoins excluded): {top_pairs}")
            return top_pairs

        except Exception as exc:
            logger.error(f"Failed to get top volume pairs: {exc}")
            # Fallback to static list
            fallback_coins = self._coins_cfg.get("all_grid_coins", [])
            # Filter stablecoins from fallback too
            fallback_filtered = [p for p in fallback_coins if p.split("/")[0] not in self.STABLECOINS]
            logger.info(f"Using fallback list: {fallback_filtered}")
            return fallback_filtered[:top_n]

    def analyze_top_volume_pairs(self, top_n: int = 10) -> dict[str, GridConfig]:
        """Analyze top N volume pairs with tier-based grid levels.

        Args:
            top_n: Number of top volume pairs to analyze

        Returns:
            Mapping of pair → GridConfig with tier-based settings
        """
        pairs = self.get_top_volume_pairs(top_n)
        results: dict[str, GridConfig] = {}

        for rank, pair in enumerate(pairs):
            try:
                results[pair] = self.analyze(pair, rank=rank)
            except Exception as exc:
                logger.error(f"Failed to analyze {pair}: {exc}")

        return results

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _to_dataframe(self, ohlcv: list) -> pd.DataFrame:
        """Convert CCXT OHLCV list to pandas DataFrame."""
        df = pd.DataFrame(
            ohlcv,
            columns=["timestamp", "open", "high", "low", "close", "volume"],
        )
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
        df.set_index("timestamp", inplace=True)
        return df

    def _price_touch_frequency(self, df: pd.DataFrame) -> list[float]:
        """Find price levels where price touched most frequently."""
        price_range = df["high"].max() - df["low"].min()
        bin_size = price_range * self._price_bin_pct

        touches: dict[float, int] = {}
        for _, row in df.iterrows():
            price_bin = round(row["close"] / bin_size) * bin_size
            touches[price_bin] = touches.get(price_bin, 0) + 1

        # Return top 20% most touched levels
        sorted_levels = sorted(touches.items(), key=lambda x: x[1], reverse=True)
        return [level for level, count in sorted_levels[: max(5, len(sorted_levels) // 5)]]

    def _volume_poc(self, df: pd.DataFrame) -> list[float]:
        """Find Point of Control (price with highest volume)."""
        # Approximate: use typical price * volume
        df["typical"] = (df["high"] + df["low"] + df["close"]) / 3
        df["vol_at_price"] = df["typical"] * df["volume"]

        # Group into bins
        price_range = df["high"].max() - df["low"].min()
        bin_size = price_range * self._price_bin_pct

        vol_by_bin: dict[float, float] = {}
        for _, row in df.iterrows():
            price_bin = round(row["typical"] / bin_size) * bin_size
            vol_by_bin[price_bin] = vol_by_bin.get(price_bin, 0) + row["vol_at_price"]

        # Return top 3 volume bins
        sorted_bins = sorted(vol_by_bin.items(), key=lambda x: x[1], reverse=True)
        return [level for level, vol in sorted_bins[:3]]

    def _rejection_wicks(self, df: pd.DataFrame) -> list[float]:
        """Find prices with strong rejection wicks."""
        rejection_levels: list[float] = []

        for _, row in df.iterrows():
            body = abs(row["close"] - row["open"])
            upper_wick = row["high"] - max(row["close"], row["open"])
            lower_wick = min(row["close"], row["open"]) - row["low"]

            # Upper rejection (bearish)
            if upper_wick > body * self._wick_multiplier:
                rejection_levels.append(row["high"])

            # Lower rejection (bullish)
            if lower_wick > body * self._wick_multiplier:
                rejection_levels.append(row["low"])

        # Return unique levels (within threshold)
        if not rejection_levels:
            return []

        unique_levels: list[float] = []
        for level in sorted(rejection_levels):
            if not unique_levels or abs(level - unique_levels[-1]) / unique_levels[-1] > self._merge_threshold:
                unique_levels.append(level)

        return unique_levels[:5]  # Max 5 rejection levels

    def _fibonacci_levels(self, df: pd.DataFrame) -> list[float]:
        """Calculate Fibonacci retracement levels."""
        high = df["high"].max()
        low = df["low"].min()
        diff = high - low

        fib_ratios = [0.236, 0.382, 0.5, 0.618, 0.786]
        return [low + diff * ratio for ratio in fib_ratios]

    def _calculate_atr(self, df: pd.DataFrame, period: int = 14) -> float:
        """Calculate Average True Range for volatility measurement."""
        high_low = df["high"] - df["low"]
        high_close = abs(df["high"] - df["close"].shift())
        low_close = abs(df["low"] - df["close"].shift())

        tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
        atr = tr.rolling(window=period).mean().iloc[-1]
        return float(atr)

    def _merge_levels(self, level_groups: list[tuple[list[float], str]]) -> list[GridLevel]:
        """Merge price levels from multiple sources, tracking strength."""
        # Flatten with source tracking
        all_with_source: list[tuple[float, str]] = []
        for levels, source in level_groups:
            for level in levels:
                all_with_source.append((level, source))

        if not all_with_source:
            return []

        # Sort by price
        all_with_source.sort(key=lambda x: x[0])

        # Merge close levels
        merged: list[GridLevel] = []
        for price, source in all_with_source:
            # Check if close to existing level
            found = False
            for existing in merged:
                if abs(price - existing["price"]) / existing["price"] <= self._merge_threshold:
                    # Merge: increment strength and add source
                    if source not in existing["sources"]:
                        existing["sources"].append(source)
                        existing["strength"] = len(existing["sources"])
                    found = True
                    break

            if not found:
                merged.append(
                    GridLevel(
                        price=round(price, 6),
                        strength=1,
                        sources=[source],
                    )
                )

        # Sort by strength (descending) then by price
        merged.sort(key=lambda x: (-x["strength"], x["price"]))
        return merged

    def _get_position_size(self, pair: str, rank: int = 0) -> float:
        """Return per-level USDC position size based on tier ranking."""
        # Get tier allocation from capital manager
        try:
            from custom_modules.capital_manager import CapitalManager

            cm = CapitalManager(dry_run=True)
            allocation = cm.get_tier_allocation(rank)
            return allocation["per_level_usdc"]
        except Exception:
            # Fallback: simple tier-based calculation
            total_capital = 1000  # Default
            grid_capital = total_capital * 0.6  # 60% for grid

            if rank < 3:
                tier_allocation = grid_capital * 0.40 / 10  # Tier 1: 40% / 10 levels
            elif rank < 6:
                tier_allocation = grid_capital * 0.30 / 8   # Tier 2: 30% / 8 levels
            else:
                tier_allocation = grid_capital * 0.20 / 6   # Tier 3: 20% / 6 levels

            return round(tier_allocation, 2)

    def _save(self, pair: str, config: GridConfig) -> None:
        """Save grid config to base_grid.json."""
        try:
            self.BASE_GRID_FILE.parent.mkdir(parents=True, exist_ok=True)

            # Load existing
            data: dict = {}
            if self.BASE_GRID_FILE.exists():
                with open(self.BASE_GRID_FILE, encoding="utf-8") as f:
                    data = json.load(f)

            # Update
            data[pair] = config

            # Save
            with open(self.BASE_GRID_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)

        except Exception as exc:
            logger.error(f"Failed to save grid for {pair}: {exc}")
