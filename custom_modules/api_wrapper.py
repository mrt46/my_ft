"""Resilient API Wrapper for Binance Exchange.

This module wraps ccxt's Binance exchange with automatic retry,
exponential backoff, local in-memory caching, health monitoring,
and graceful error handling for all API interactions.
"""

import asyncio
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, TypedDict

import ccxt
import yaml
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# TypedDicts
# ---------------------------------------------------------------------------

class CacheEntry(TypedDict):
    """Single cache entry with data and timestamp."""

    data: Any
    timestamp: float


class HealthStatus(TypedDict):
    """Current health state of the exchange connection."""

    binance: bool
    last_check: float
    error_count: int


# ---------------------------------------------------------------------------
# Main Wrapper
# ---------------------------------------------------------------------------

class ResilientExchangeWrapper:
    """Fault-tolerant Binance exchange API wrapper.

    Features:
        - Automatic retry with exponential backoff (up to 5 attempts)
        - In-memory cache with 1-hour TTL and fallback on network errors
        - Health monitoring (ping every 30 s)
        - Intelligent order error handling (InsufficientFunds, InvalidOrder, …)
        - Telegram alerts on critical failures

    Example:
        exchange = ResilientExchangeWrapper()
        ohlcv = exchange.fetch_ohlcv('BTC/USDC', '1h', limit=200)
        order = exchange.execute_order('BTC/USDC', 'buy', 0.001, price=50000)
    """

    def __init__(self) -> None:
        """Initialise exchange connection, cache, and load settings."""
        self._cache: dict[str, CacheEntry] = {}
        self.health_status: HealthStatus = {
            "binance": True,
            "last_check": 0.0,
            "error_count": 0,
        }

        settings_path = Path(__file__).parent.parent / "config" / "settings.yaml"
        with open(settings_path) as fh:
            cfg = yaml.safe_load(fh)

        api_cfg = cfg.get("api", {})
        self._max_attempts: int = api_cfg.get("retry_max_attempts", 5)
        self._cache_ttl: int = api_cfg.get("cache_ttl_seconds", 3600)
        self._rate_limit_wait: int = api_cfg.get("rate_limit_wait_seconds", 60)
        self._base_wait: int = api_cfg.get("retry_base_wait_seconds", 1)

        self.exchange = ccxt.binance(
            {
                "apiKey": os.getenv("BINANCE_API_KEY"),
                "secret": os.getenv("BINANCE_API_SECRET"),
                "enableRateLimit": True,
                "options": {
                    "defaultType": "spot",
                    "adjustForTimeDifference": True,
                },
            }
        )
        logger.info("ResilientExchangeWrapper initialised")

    # ------------------------------------------------------------------
    # Cache helpers
    # ------------------------------------------------------------------

    def _cache_key(self, fn: str, *args: Any) -> str:
        return f"{fn}_{'_'.join(str(a) for a in args)}"

    def _cache_valid(self, key: str) -> bool:
        if key not in self._cache:
            return False
        return (time.time() - self._cache[key]["timestamp"]) < self._cache_ttl

    def _set_cache(self, key: str, data: Any) -> None:
        self._cache[key] = {"data": data, "timestamp": time.time()}

    # ------------------------------------------------------------------
    # Retry engine
    # ------------------------------------------------------------------

    def _call(self, fn, *args, **kwargs) -> Any:
        """Execute *fn* with exponential backoff retry.

        Args:
            fn: Callable to execute.
            *args: Positional arguments forwarded to *fn*.
            **kwargs: Keyword arguments forwarded to *fn*.

        Returns:
            Return value of *fn*.

        Raises:
            Last caught exception after all retry attempts are exhausted.
        """
        last_exc: Exception = RuntimeError("No attempts made")

        for attempt in range(1, self._max_attempts + 1):
            try:
                result = fn(*args, **kwargs)
                self.health_status["error_count"] = 0
                return result

            except ccxt.RateLimitExceeded as exc:
                logger.warning(
                    f"Rate limit (attempt {attempt}/{self._max_attempts}), "
                    f"waiting {self._rate_limit_wait}s …"
                )
                time.sleep(self._rate_limit_wait)
                last_exc = exc

            except ccxt.RequestTimeout as exc:
                wait = self._base_wait * (2 ** (attempt - 1))
                logger.warning(f"Timeout (attempt {attempt}), retry in {wait}s …")
                time.sleep(wait)
                last_exc = exc

            except ccxt.NetworkError as exc:
                wait = self._base_wait * (2 ** (attempt - 1))
                logger.error(f"Network error (attempt {attempt}): {exc}, retry in {wait}s …")
                time.sleep(wait)
                last_exc = exc

            except (ccxt.InsufficientFunds, ccxt.InvalidOrder, ccxt.OrderNotFound):
                raise  # Non-retriable — propagate immediately

            except Exception as exc:
                logger.error(f"Unexpected error (attempt {attempt}): {exc}")
                last_exc = exc
                break

        self.health_status["error_count"] += 1
        raise last_exc

    # ------------------------------------------------------------------
    # Public API — data fetching
    # ------------------------------------------------------------------

    def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str,
        limit: int = 500,
    ) -> list[list]:
        """Fetch OHLCV candlestick data with transparent cache fallback.

        Args:
            symbol: Trading pair, e.g. ``'BTC/USDC'``.
            timeframe: Candle period, e.g. ``'1m'``, ``'4h'``, ``'1d'``.
            limit: Number of candles to retrieve.

        Returns:
            List of ``[timestamp, open, high, low, close, volume]`` rows.

        Raises:
            ccxt.NetworkError: When network fails and no valid cache exists.
        """
        key = self._cache_key("ohlcv", symbol, timeframe, limit)
        try:
            data = self._call(self.exchange.fetch_ohlcv, symbol, timeframe, limit=limit)
            self._set_cache(key, data)
            logger.debug(f"fetch_ohlcv {symbol} {timeframe}: {len(data)} candles")
            return data

        except (ccxt.NetworkError, ccxt.RequestTimeout) as exc:
            if self._cache_valid(key):
                age_min = (time.time() - self._cache[key]["timestamp"]) / 60
                logger.warning(f"Fallback cache for {symbol} ({age_min:.0f} min old)")
                self._alert(f"⚠️ {symbol} {timeframe} için cache data kullanılıyor ({age_min:.0f}dk)")
                return self._cache[key]["data"]
            logger.error(f"fetch_ohlcv {symbol}: network error, no cache — {exc}")
            raise

    def fetch_ticker(self, symbol: str) -> dict:
        """Fetch current ticker (last price, bid, ask, 24 h volume).

        Args:
            symbol: Trading pair.

        Returns:
            ccxt ticker dict.
        """
        key = self._cache_key("ticker", symbol)
        try:
            data = self._call(self.exchange.fetch_ticker, symbol)
            self._set_cache(key, data)
            return data
        except (ccxt.NetworkError, ccxt.RequestTimeout):
            if self._cache_valid(key):
                logger.warning(f"Fallback cache ticker for {symbol}")
                return self._cache[key]["data"]
            raise

    def fetch_balance(self) -> dict:
        """Fetch full account balance.

        Returns:
            Balance dict structured as ``{currency: {free, used, total}}``.

        Raises:
            ccxt.ExchangeError: On exchange-side failure.
        """
        try:
            balance = self._call(self.exchange.fetch_balance)
            logger.debug("fetch_balance: OK")
            return balance
        except Exception as exc:
            logger.error(f"fetch_balance failed: {exc}")
            raise

    def fetch_open_orders(self, symbol: str | None = None) -> list[dict]:
        """Fetch all open orders, optionally for a single symbol.

        Args:
            symbol: Optional pair filter. ``None`` fetches all pairs.

        Returns:
            List of open order dicts.
        """
        try:
            return self._call(self.exchange.fetch_open_orders, symbol)
        except Exception as exc:
            logger.error(f"fetch_open_orders failed: {exc}")
            return []

    # ------------------------------------------------------------------
    # Public API — order management
    # ------------------------------------------------------------------

    def execute_order(
        self,
        symbol: str,
        side: str,
        amount: float,
        price: float | None = None,
        order_type: str = "market",
    ) -> dict | None:
        """Place a buy or sell order with intelligent error recovery.

        Args:
            symbol: Trading pair, e.g. ``'BTC/USDC'``.
            side: ``'buy'`` or ``'sell'``.
            amount: Quantity in base currency.
            price: Limit price. ``None`` for market orders.
            order_type: ``'market'`` (default) or ``'limit'``.

        Returns:
            Filled order dict, or ``None`` on failure.
        """
        try:
            if order_type == "limit" and price is not None:
                order = self._call(
                    self.exchange.create_limit_order, symbol, side, amount, price
                )
            else:
                order = self._call(
                    self.exchange.create_market_order, symbol, side, amount
                )
            logger.info(f"Order OK: {side} {amount:.6f} {symbol} @ {price or 'market'}")
            return order

        except ccxt.InsufficientFunds:
            logger.error(f"Insufficient funds: {side} {amount} {symbol}")
            self._alert(f"❌ Yetersiz bakiye: {symbol} {side} {amount}")
            return None

        except ccxt.InvalidOrder as exc:
            logger.error(f"Invalid order {symbol}: {exc}")
            # Auto-fix MIN_NOTIONAL
            if price and "MIN_NOTIONAL" in str(exc):
                try:
                    markets = self.exchange.load_markets()
                    min_cost = (
                        markets.get(symbol, {})
                        .get("limits", {})
                        .get("cost", {})
                        .get("min", 10.0)
                    )
                    adjusted = (min_cost * 1.1) / price
                    logger.info(f"Auto-fix MIN_NOTIONAL -> {adjusted:.6f}")
                    return self.execute_order(symbol, side, adjusted, price, order_type)
                except Exception as retry_exc:
                    logger.error(f"Auto-fix failed: {retry_exc}")
            self._alert(f"❌ Geçersiz emir: {symbol} — {exc}")
            return None

        except ccxt.ExchangeError as exc:
            logger.error(f"Exchange error {symbol}: {exc}")
            self._alert(f"🚨 Exchange hatası: {symbol} — {exc}")
            return None

    def cancel_order(self, order_id: str, symbol: str) -> bool:
        """Cancel an open order by ID.

        Args:
            order_id: Exchange-assigned order identifier.
            symbol: Trading pair the order belongs to.

        Returns:
            ``True`` if cancelled, ``False`` if already filled/not found.
        """
        try:
            self._call(self.exchange.cancel_order, order_id, symbol)
            logger.info(f"Order cancelled: {order_id} ({symbol})")
            return True
        except ccxt.OrderNotFound:
            logger.warning(f"Order not found (may be filled): {order_id}")
            return False
        except Exception as exc:
            logger.error(f"cancel_order {order_id}: {exc}")
            return False

    # ------------------------------------------------------------------
    # Health monitoring
    # ------------------------------------------------------------------

    def health_check(self) -> dict:
        """Ping the exchange and return health status.

        Returns:
            Dict with keys ``status``, ``latency_ms``, ``timestamp``.
            ``status`` is one of ``'healthy'`` | ``'critical'``.
        """
        start = time.time()
        try:
            self.exchange.fetch_time()
            latency = (time.time() - start) * 1000
            self.health_status.update({"binance": True, "last_check": time.time()})
            logger.debug(f"Health check OK: {latency:.0f} ms")
            return {"status": "healthy", "latency_ms": round(latency, 1), "timestamp": time.time()}

        except Exception as exc:
            self.health_status.update({"binance": False, "last_check": time.time()})
            logger.critical(f"Health check FAILED: {exc}")
            self._alert("🚨 CRITICAL: Binance API erişilemiyor!")
            return {"status": "critical", "error": str(exc), "timestamp": time.time()}

    async def monitor_websocket(self) -> None:
        """Continuously monitor exchange health; auto-reconnect on failure.

        Runs as an asyncio coroutine. Intended to be launched with
        ``asyncio.create_task(wrapper.monitor_websocket())``.
        """
        logger.info("WebSocket monitor started")
        delay = 5
        max_delay = 60

        while True:
            try:
                result = await asyncio.to_thread(self.health_check)
                if result["status"] != "healthy":
                    raise ConnectionError("Health check failed")
                delay = 5
                await asyncio.sleep(30)

            except (ConnectionError, Exception) as exc:
                logger.warning(f"Connection issue: {exc}, reconnect in {delay}s …")
                self._alert(f"⚠️ Bağlantı hatası, {delay}s sonra yeniden bağlanılıyor …")
                await asyncio.sleep(delay)
                delay = min(delay * 2, max_delay)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _alert(self, message: str) -> None:
        """Fire-and-forget Telegram alert (avoids circular import)."""
        try:
            from custom_modules.telegram_bot import send_alert_sync  # noqa: PLC0415

            send_alert_sync(message)
        except Exception:
            logger.warning(f"Telegram alert skipped: {message}")


# ---------------------------------------------------------------------------
# Global exception handler
# ---------------------------------------------------------------------------

def setup_global_exception_handler() -> None:
    """Install a global handler for unhandled exceptions.

    Logs the traceback and sends a Telegram alert before the process exits.
    Safe to call multiple times (idempotent).
    """

    def _handler(exc_type, exc_value, exc_traceback) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_traceback)
            return

        logger.critical(
            "Uncaught exception — bot stopped",
            exc_info=(exc_type, exc_value, exc_traceback),
        )
        try:
            from custom_modules.telegram_bot import send_alert_sync  # noqa: PLC0415

            send_alert_sync(
                f"🚨 CRITICAL ERROR\n\n"
                f"Type: {exc_type.__name__}\n"
                f"Message: {exc_value}\n\n"
                f"Bot durdu, manuel kontrol gerekli!"
            )
        except Exception:
            pass

    sys.excepthook = _handler
    logger.info("Global exception handler installed")
