"""Crypto Screener — multi-timeframe oversold scanner for USDC pairs.

Çalışma koşulları (tümü sağlanmalı):
  - 24h hacim > 5M USDC
  - Fiyat < EMA200 (1H)  AND  < EMA200 (4H)  AND  < EMA200 (1D)
  - RSI_1h < 40  OR  RSI_4h < 35  OR  RSI_1d < 30  (en az biri oversold)
  - Fırsat skoru > min_score (varsayılan 40)

Skorlama (max 120 puan):
  - RSI 1h  : 0–20 puan
  - RSI 4h  : 0–25 puan
  - RSI 1d  : 0–35 puan
  - EMA mesafesi (1d): 0–20 puan
  - Hacim   : 0–20 puan

Sonuçlar ``data/screener_queue.json`` dosyasına kaydedilir ve
Telegram'a alım önerisiyle birlikte gönderilir.
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
    """Single screener result entry — multi-timeframe."""

    pair: str
    price: float

    # RSI — tüm timeframe'ler
    rsi_1h: float
    rsi_4h: float
    rsi_1d: float

    # EMA200 — tüm timeframe'ler
    ema200_1h: float
    ema200_4h: float
    ema200_1d: float

    # EMA mesafeleri (% olarak, pozitif = EMA altında)
    distance_pct_1h: float
    distance_pct_4h: float
    distance_pct_1d: float

    # Eski isimler (geriye uyumluluk)
    ema200: float          # ema200_1d ile aynı
    distance_pct: float    # distance_pct_1d ile aynı

    volume: float          # 24h USDC hacmi
    score: int             # 0–120

    signal_strength: str   # "GÜÇLÜ 🔴" | "ORTA 🟡" | "ZAYIF 🟢"
    buy_suggestion: str    # "HEMEN AL" | "AL" | "KISMİ AL" | "İZLE"

    timestamp: float


# ---------------------------------------------------------------------------
# Screener
# ---------------------------------------------------------------------------

class Screener:
    """Multi-timeframe crypto screener for Binance USDC pairs.

    Çalıştırma:
        screener = Screener(exchange_wrapper)
        candidates = screener.daily_screener()
    """

    QUEUE_FILE = Path(__file__).parent.parent / "data" / "screener_queue.json"

    def __init__(self, exchange_wrapper) -> None:
        self._exchange = exchange_wrapper

        settings_path = Path(__file__).parent.parent / "config" / "settings.yaml"
        with open(settings_path) as fh:
            cfg = yaml.safe_load(fh)

        s = cfg.get("screener", {})
        self._volume_min: float = s.get("volume_min_24h", 5_000_000)
        self._rsi_1h_thr: float = s.get("rsi_1h_threshold", 40)
        self._rsi_4h_thr: float = s.get("rsi_4h_threshold", 35)
        self._rsi_1d_thr: float = s.get("rsi_1d_threshold", 30)
        self._ema_period: int = s.get("ema200_period", 200)
        self._min_score: int = s.get("min_score", 40)
        self._top_n: int = s.get("top_n", 5)

        logger.info("Screener initialised (multi-TF: 1h+4h+1d EMA200)")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def daily_screener(self) -> list[ScreenerCandidate]:
        """Tüm Binance USDC çiftlerini tarar, oversold fırsatları listeler.

        Returns:
            Top N ScreenerCandidate listesi (skora göre azalan sıralı).
        """
        logger.info("Daily screener started (multi-TF 1h+4h+1d EMA200)")
        start = time.time()

        all_pairs = self._get_all_usdc_pairs()
        logger.info(f"Scanning {len(all_pairs)} USDC pairs …")

        candidates: list[ScreenerCandidate] = []

        for pair in all_pairs:
            try:
                candidate = self._evaluate_pair(pair)
                if candidate:
                    candidates.append(candidate)
            except Exception as exc:
                logger.warning(f"Screener skipped {pair}: {exc}")

        qualified = [c for c in candidates if c["score"] >= self._min_score]
        top = sorted(qualified, key=lambda x: x["score"], reverse=True)[: self._top_n]

        elapsed = time.time() - start
        logger.info(
            "Screener done: %d candidates → %d qualified → %d top — %.1fs",
            len(candidates), len(qualified), len(top), elapsed,
        )

        if len(candidates) == 0:
            logger.info(
                "Screener: 0 aday. Büyük ihtimalle piyasa yükseliş trendinde "
                "(coinlerin çoğu 1h/4h/1d EMA200 üzerinde). Normal, yarın tekrar çalışacak."
            )

        self._save_queue(top)
        return top

    def calculate_opportunity_score(
        self,
        rsi_1h: float,
        rsi_4h: float,
        rsi_1d: float,
        distance_ema_1d: float,
        volume: float,
    ) -> int:
        """0–120 arasında fırsat skoru hesaplar.

        Args:
            rsi_1h: 1 saatlik RSI.
            rsi_4h: 4 saatlik RSI.
            rsi_1d: Günlük RSI.
            distance_ema_1d: EMA200 (1D) altında yüzde mesafe (pozitif = altında).
            volume: 24h USDC hacmi.

        Returns:
            Tamsayı skor. 80+ mükemmel, 60-79 iyi, 40-59 orta.
        """
        score = 0

        # RSI 1h (max 20 puan)
        if rsi_1h < 25:
            score += 20
        elif rsi_1h < 30:
            score += 15
        elif rsi_1h < 35:
            score += 10
        elif rsi_1h < 40:
            score += 5

        # RSI 4h (max 25 puan)
        if rsi_4h < 25:
            score += 25
        elif rsi_4h < 30:
            score += 18
        elif rsi_4h < 35:
            score += 10

        # RSI 1d (max 35 puan)
        if rsi_1d < 25:
            score += 35
        elif rsi_1d < 30:
            score += 25
        elif rsi_1d < 35:
            score += 15

        # EMA200 (1D) mesafesi (max 20 puan — ideal: %3-15)
        if 3 <= distance_ema_1d < 8:
            score += 20   # Optimal bölge
        elif 8 <= distance_ema_1d < 15:
            score += 12
        elif distance_ema_1d < 3:
            score += 3    # Çok yakın, riskli
        elif distance_ema_1d < 25:
            score += 5

        # Hacim (max 20 puan)
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
        """Fırsat skoru ve hacime göre dinamik pozisyon büyüklüğü hesaplar.

        Returns:
            USDC cinsinden pozisyon büyüklüğü, [20, 100] aralığında.
        """
        score = candidate["score"]
        volume = candidate["volume"]

        if score >= 80:
            base = 100.0
        elif score >= 60:
            base = 60.0
        else:
            base = 30.0

        if volume > 50_000_000:
            multiplier = 1.2
        elif volume < 10_000_000:
            multiplier = 0.8
        else:
            multiplier = 1.0

        final = base * multiplier
        final = min(final, available_usdc, 100.0)
        final = max(final, 20.0)
        return round(final, 2)

    # ------------------------------------------------------------------
    # Pair evaluation
    # ------------------------------------------------------------------

    def _evaluate_pair(self, pair: str) -> ScreenerCandidate | None:
        """Tek bir çifti tüm kriterler açısından değerlendirir.

        Args:
            pair: Trading pair, e.g. ``'AVAX/USDC'``.

        Returns:
            ScreenerCandidate if all criteria are met, None otherwise.
        """
        # --- Hacim ön filtresi (hızlı) ---
        ticker = self._exchange.fetch_ticker(pair)
        volume_24h = float(ticker.get("quoteVolume", 0))
        if volume_24h < self._volume_min:
            return None

        current_price = float(ticker.get("last", 0))
        if current_price <= 0:
            return None

        # --- 1h candle'lar (RSI 1h + EMA200 1h) ---
        raw_1h = self._exchange.fetch_ohlcv(pair, "1h", limit=220)
        df_1h = self._to_df(raw_1h)
        rsi_1h = self._calculate_rsi(df_1h, period=14)
        ema200_1h = self._calculate_ema(df_1h, period=self._ema_period)

        # --- 4h candle'lar (RSI 4h + EMA200 4h) ---
        raw_4h = self._exchange.fetch_ohlcv(pair, "4h", limit=210)
        df_4h = self._to_df(raw_4h)
        rsi_4h = self._calculate_rsi(df_4h, period=14)
        ema200_4h = self._calculate_ema(df_4h, period=self._ema_period)

        # --- 1d candle'lar (RSI 1d + EMA200 1d) ---
        raw_1d = self._exchange.fetch_ohlcv(pair, "1d", limit=250)
        df_1d = self._to_df(raw_1d)
        rsi_1d = self._calculate_rsi(df_1d, period=14)
        ema200_1d = self._calculate_ema(df_1d, period=self._ema_period)

        # --- EMA200 filtresi: HER ÜÇ TIMEFRAME'DE ALTINDA OLMALI ---
        if current_price >= ema200_1h:
            return None
        if current_price >= ema200_4h:
            return None
        if current_price >= ema200_1d:
            return None

        # --- RSI filtresi: en az bir TF oversold ---
        rsi_ok = (
            rsi_1h < self._rsi_1h_thr
            or rsi_4h < self._rsi_4h_thr
            or rsi_1d < self._rsi_1d_thr
        )
        if not rsi_ok:
            return None

        # --- Mesafeler ---
        distance_1h = ((ema200_1h - current_price) / current_price) * 100
        distance_4h = ((ema200_4h - current_price) / current_price) * 100
        distance_1d = ((ema200_1d - current_price) / current_price) * 100

        # --- Skor ---
        score = self.calculate_opportunity_score(
            rsi_1h, rsi_4h, rsi_1d, distance_1d, volume_24h
        )

        # --- Sinyal gücü ---
        oversold_count = sum([
            rsi_1h < self._rsi_1h_thr,
            rsi_4h < self._rsi_4h_thr,
            rsi_1d < self._rsi_1d_thr,
        ])

        if oversold_count == 3 and score >= 70:
            signal_strength = "GÜÇLÜ 🔴"
            buy_suggestion = "HEMEN AL"
        elif oversold_count >= 2 and score >= 55:
            signal_strength = "ORTA 🟡"
            buy_suggestion = "AL"
        elif oversold_count >= 1 and score >= 40:
            signal_strength = "ZAYIF 🟢"
            buy_suggestion = "KISMİ AL / İZLE"
        else:
            signal_strength = "ÇOK ZAYIF ⚪"
            buy_suggestion = "İZLE"

        return ScreenerCandidate(
            pair=pair,
            price=round(current_price, 8),
            rsi_1h=round(rsi_1h, 2),
            rsi_4h=round(rsi_4h, 2),
            rsi_1d=round(rsi_1d, 2),
            ema200_1h=round(ema200_1h, 8),
            ema200_4h=round(ema200_4h, 8),
            ema200_1d=round(ema200_1d, 8),
            distance_pct_1h=round(distance_1h, 2),
            distance_pct_4h=round(distance_4h, 2),
            distance_pct_1d=round(distance_1d, 2),
            # Geriye uyumluluk
            ema200=round(ema200_1d, 8),
            distance_pct=round(distance_1d, 2),
            volume=round(volume_24h, 0),
            score=score,
            signal_strength=signal_strength,
            buy_suggestion=buy_suggestion,
            timestamp=time.time(),
        )

    # ------------------------------------------------------------------
    # Technical indicators
    # ------------------------------------------------------------------

    def _calculate_rsi(self, df: pd.DataFrame, period: int = 14) -> float:
        """Wilder smoothing yöntemiyle RSI hesaplar."""
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
        """EMA hesaplar."""
        ema = df["close"].ewm(span=period, adjust=False).mean()
        return float(ema.iloc[-1])

    # ------------------------------------------------------------------
    # Exchange helpers
    # ------------------------------------------------------------------

    def _get_all_usdc_pairs(self) -> list[str]:
        """Binance'deki aktif USDC spot çiftlerini döner — stablecoinler hariç."""
        # Stablecoin base'leri — bunlar USDC karşısında hiçbir zaman trade edilmez
        stablecoins = {
            "USDT", "USDC", "BUSD", "DAI", "TUSD", "FDUSD", "USD1",
            "USDD", "UST", "USDP", "GUSD", "HUSD", "SUSD", "EURC",
            "PYUSD", "FRAX", "LUSD", "MIM", "CRVUSD", "USDE",
        }
        try:
            markets = self._exchange.exchange.load_markets()
            return [
                symbol
                for symbol, market in markets.items()
                if (
                    market.get("quote") == "USDC"
                    and market.get("active", False)
                    and market.get("spot", False)
                    and market.get("base") not in stablecoins
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
