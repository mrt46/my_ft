"""Crypto Screener — daily scanner for oversold Binance USDC pairs.

Runs once at 00:00 UTC, scans all ~200+ USDC pairs for oversold
conditions relative to EMA200, scores each candidate, and returns
the top 5. Results are queued for Telegram approval and stored in
``data/screener_queue.json``.
"""

import json
import logging
import time
from pathlib import Path
from typing import TypedDict

import pandas as pd
import yaml

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# TypedDicts
# ---------------------------------------------------------------------------

class ScreenerCandidate(TypedDict):
    """Single screener result entry."""

    pair: str
    price: float
    rsi_4h: float
    rsi_1d: float
    ema200: float
    distance_pct: float    # % below EMA200
    volume: float          # 24 h USDC volume
    score: int
    timestamp: float


# ---------------------------------------------------------------------------
# Screener
# ---------------------------------------------------------------------------

class Screener:
    """Daily crypto screener for Binance USDC pairs.

    Criteria (flexible — pair must satisfy ALL of):
        - 24 h volume > ``volume_min_24h`` (default 5M USDC)
        - RSI_4h < 35 OR RSI_1d < 30  (at least one oversold)
        - Current price < EMA200 (1D)
        - Opportunity score > ``min_score`` (default 40)

    Scoring:
        - RSI_4h contribution: 0–30 pts
        - RSI_1d contribution: 0–40 pts
        - Distance to EMA200: 0–25 pts (sweet spot 3–15%)
        - Volume: 0–20 pts

    Example:
        screener = Screener(exchange_wrapper)
        candidates = screener.daily_screener()
    """

    QUEUE_FILE = Path(__file__).parent.parent / "data" / "screener_queue.json"

    def __init__(self, exchange_wrapper) -> None:
        """Initialise with a live exchange wrapper.

        Args:
            exchange_wrapper: Instance of ``ResilientExchangeWrapper``.
        """
        self._exchange = exchange_wrapper

        settings_path = Path(__file__).parent.parent / "config" / "settings.yaml"
        with open(settings_path) as fh:
            cfg = yaml.safe_load(fh)

        s = cfg.get("screener", {})
        self._volume_min: float = s.get("volume_min_24h", 5_000_000)
        self._rsi_4h_thr: float = s.get("rsi_4h_threshold", 35)
        self._rsi_1d_thr: float = s.get("rsi_1d_threshold", 30)
        self._ema_period: int = s.get("ema200_period", 200)
        self._min_score: int = s.get("min_score", 40)
        self._top_n: int = s.get("top_n", 5)

        logger.info("Screener initialised")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def daily_screener(self) -> list[ScreenerCandidate]:
        """Scan all Binance USDC pairs for oversold opportunities.

        Runs at 00:00 UTC daily. Execution time: ~2–3 minutes.

        Returns:
            Top 5 ScreenerCandidates sorted by score (descending).
        """
        logger.info("Daily screener started")
        start = time.time()

        all_pairs = self._get_all_usdc_pairs()
        logger.info(f"Scanning {len(all_pairs)} USDC pairs ...")

        candidates: list[ScreenerCandidate] = []

        for pair in all_pairs:
            try:
                candidate = self._evaluate_pair(pair)
                if candidate:
                    candidates.append(candidate)
            except Exception as exc:
                logger.warning(f"Screener skipped {pair}: {exc}")

        # Filter by minimum score and sort
        qualified = [c for c in candidates if c["score"] >= self._min_score]
        top = sorted(qualified, key=lambda x: x["score"], reverse=True)[: self._top_n]

        elapsed = time.time() - start
        logger.info(
            f"Screener done: {len(candidates)} candidates -> {len(qualified)} qualified "
            f"-> {len(top)} top - {elapsed:.1f}s"
        )
        if len(candidates) == 0:
            logger.info(
                "Screener found 0 candidates. Likely reason: market is in a bullish trend "
                f"(most coins above EMA200 or RSI_4h >= {self._rsi_4h_thr} and "
                f"RSI_1d >= {self._rsi_1d_thr}). This is normal — screener will retry tomorrow."
            )

        self._save_queue(top)
        return top

    def calculate_opportunity_score(
        self,
        rsi_4h: float,
        rsi_1d: float,
        distance_ema: float,
        volume: float,
    ) -> int:
        """Compute a 0–115 opportunity score for a screener candidate.

        Args:
            rsi_4h: 4-hour RSI value.
            rsi_1d: 1-day RSI value.
            distance_ema: Percentage below EMA200 (positive = below).
            volume: 24 h traded USDC volume.

        Returns:
            Integer score. 80+ = excellent, 60–79 = good, 40–59 = medium.
        """
        score = 0

        # RSI 4h (max 30 pts)
        if rsi_4h < 25:
            score += 30
        elif rsi_4h < 30:
            score += 20
        elif rsi_4h < 35:
            score += 10

        # RSI 1d (max 40 pts)
        if rsi_1d < 25:
            score += 40
        elif rsi_1d < 30:
            score += 30
        elif rsi_1d < 35:
            score += 15

        # EMA distance (max 25 pts — sweet spot 3–15%)
        if distance_ema < 3:
            score += 5    # Too close — risky false signal
        elif distance_ema < 8:
            score += 25   # Optimal
        elif distance_ema < 15:
            score += 15
        elif distance_ema < 25:
            score += 5

        # Volume (max 20 pts)
        if volume > 50_000_000:
            score += 20
        elif volume > 20_000_000:
            score += 15
        elif volume > 10_000_000:
            score += 10
        elif volume > 5_000_000:
            score += 5

        return score

    def calculate_screener_position_size(
        self,
        candidate: ScreenerCandidate,
        available_usdc: float,
    ) -> float:
        """Compute dynamic position size based on opportunity score.

        Args:
            candidate: ScreenerCandidate with ``score`` and ``volume``.
            available_usdc: Free USDC available for trading.

        Returns:
            Position size in USDC, clamped to [20, 100].
        """
        score = candidate["score"]
        volume = candidate["volume"]

        # Base amount by score
        if score >= 80:
            base = 100.0
        elif score >= 60:
            base = 60.0
        else:
            base = 30.0

        # Liquidity multiplier
        if volume > 50_000_000:
            multiplier = 1.2
        elif volume < 10_000_000:
            multiplier = 0.8
        else:
            multiplier = 1.0

        final = base * multiplier
        final = min(final, available_usdc)
        final = max(final, 20.0)
        final = min(final, 100.0)
        return round(final, 2)

    # ------------------------------------------------------------------
    # Internal: pair evaluation
    # ------------------------------------------------------------------

    def _evaluate_pair(self, pair: str) -> ScreenerCandidate | None:
        """Evaluate a single pair against all screener criteria.

        Args:
            pair: Trading pair, e.g. ``'MATIC/USDC'``.

        Returns:
            ScreenerCandidate if criteria are met, None otherwise.
        """
        # --- Volume check (quick pre-filter using ticker) ---
        ticker = self._exchange.fetch_ticker(pair)
        volume_24h = float(ticker.get("quoteVolume", 0))

        if volume_24h < self._volume_min:
            return None

        current_price = float(ticker.get("last", 0))
        if current_price <= 0:
            return None

        # --- Fetch 4h candles for RSI_4h ---
        raw_4h = self._exchange.fetch_ohlcv(pair, "4h", limit=50)
        df_4h = self._to_df(raw_4h)
        rsi_4h = self._calculate_rsi(df_4h, period=14)

        # --- Fetch 1d candles for RSI_1d and EMA200 ---
        raw_1d = self._exchange.fetch_ohlcv(pair, "1d", limit=250)
        df_1d = self._to_df(raw_1d)
        rsi_1d = self._calculate_rsi(df_1d, period=14)
        ema200 = self._calculate_ema(df_1d, period=self._ema_period)

        # --- Apply filters ---
        rsi_ok = rsi_4h < self._rsi_4h_thr or rsi_1d < self._rsi_1d_thr
        if not rsi_ok:
            return None

        if current_price >= ema200:
            return None

        distance_pct = ((ema200 - current_price) / current_price) * 100
        score = self.calculate_opportunity_score(rsi_4h, rsi_1d, distance_pct, volume_24h)

        return ScreenerCandidate(
            pair=pair,
            price=round(current_price, 8),
            rsi_4h=round(rsi_4h, 2),
            rsi_1d=round(rsi_1d, 2),
            ema200=round(ema200, 8),
            distance_pct=round(distance_pct, 2),
            volume=round(volume_24h, 0),
            score=score,
            timestamp=time.time(),
        )

    # ------------------------------------------------------------------
    # Technical indicator helpers
    # ------------------------------------------------------------------

    def _calculate_rsi(self, df: pd.DataFrame, period: int = 14) -> float:
        """Calculate RSI using Wilder's smoothing method.

        Args:
            df: OHLCV DataFrame with a ``close`` column.
            period: RSI period (default 14).

        Returns:
            Most recent RSI value (0–100).
        """
        close = df["close"]
        delta = close.diff()

        gain = delta.clip(lower=0)
        loss = (-delta).clip(lower=0)

        avg_gain = gain.ewm(com=period - 1, adjust=False).mean()
        avg_loss = loss.ewm(com=period - 1, adjust=False).mean()

        rs = avg_gain / avg_loss.replace(0, float("inf"))
        rsi = 100 - (100 / (1 + rs))
        return float(rsi.iloc[-1])

    def _calculate_ema(self, df: pd.DataFrame, period: int = 200) -> float:
        """Calculate EMA for the specified period.

        Args:
            df: OHLCV DataFrame with a ``close`` column.
            period: EMA period (default 200).

        Returns:
            Most recent EMA value.
        """
        ema = df["close"].ewm(span=period, adjust=False).mean()
        return float(ema.iloc[-1])

    # ------------------------------------------------------------------
    # Exchange helpers
    # ------------------------------------------------------------------

    def _get_all_usdc_pairs(self) -> list[str]:
        """Fetch all USDC spot trading pairs from Binance.

        Returns:
            List of pair strings, e.g. ``['BTC/USDC', 'ETH/USDC', …]``.
        """
        try:
            markets = self._exchange.exchange.load_markets()
            return [
                symbol
                for symbol, market in markets.items()
                if (
                    market.get("quote") == "USDC"
                    and market.get("active", False)
                    and market.get("spot", False)
                )
            ]
        except Exception as exc:
            logger.error(f"Failed to load markets: {exc}")
            return []

    def _to_df(self, raw: list[list]) -> pd.DataFrame:
        df = pd.DataFrame(
            raw, columns=["timestamp", "open", "high", "low", "close", "volume"]
        )
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
        df.set_index("timestamp", inplace=True)
        return df.astype(float)

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _save_queue(self, candidates: list[ScreenerCandidate]) -> None:
        try:
            self.QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)
            self.QUEUE_FILE.write_text(json.dumps(candidates, indent=2))
            logger.info(f"Screener queue saved: {len(candidates)} candidates")
        except Exception as exc:
            logger.error(f"Failed to save screener queue: {exc}")
