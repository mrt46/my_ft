"""Unit tests for custom_modules.api_wrapper."""

import time
from unittest.mock import MagicMock, patch, PropertyMock

import pytest
import ccxt

from custom_modules.api_wrapper import ResilientExchangeWrapper, setup_global_exception_handler


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def wrapper(tmp_path, monkeypatch):
    """Return a wrapper with a mocked exchange and temp settings."""
    settings = tmp_path / "config" / "settings.yaml"
    settings.parent.mkdir()
    settings.write_text(
        "api:\n"
        "  retry_max_attempts: 3\n"
        "  cache_ttl_seconds: 3600\n"
        "  rate_limit_wait_seconds: 1\n"
        "  retry_base_wait_seconds: 0\n"
    )
    monkeypatch.setenv("BINANCE_API_KEY", "test_key")
    monkeypatch.setenv("BINANCE_API_SECRET", "test_secret")

    with patch("custom_modules.api_wrapper.Path") as mock_path, \
         patch("custom_modules.api_wrapper.ccxt.binance") as mock_binance:

        # Settings path redirect
        mock_path.return_value.__truediv__.return_value = settings
        mock_path.return_value.parent.parent.__truediv__.return_value = settings

        w = ResilientExchangeWrapper.__new__(ResilientExchangeWrapper)
        w._cache = {}
        w.health_status = {"binance": True, "last_check": 0.0, "error_count": 0}
        w._max_attempts = 3
        w._cache_ttl = 3600
        w._rate_limit_wait = 1
        w._base_wait = 0
        w.exchange = MagicMock()
        return w


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

class TestCacheHelpers:
    def test_cache_key_format(self, wrapper):
        key = wrapper._cache_key("ohlcv", "BTC/USDC", "1h", 100)
        assert key == "ohlcv_BTC/USDC_1h_100"

    def test_cache_invalid_when_empty(self, wrapper):
        assert wrapper._cache_valid("missing_key") is False

    def test_cache_valid_when_fresh(self, wrapper):
        wrapper._set_cache("k", {"price": 1})
        assert wrapper._cache_valid("k") is True

    def test_cache_invalid_when_expired(self, wrapper):
        wrapper._cache_ttl = 1
        wrapper._set_cache("k", {"price": 1})
        time.sleep(1.1)
        assert wrapper._cache_valid("k") is False


# ---------------------------------------------------------------------------
# fetch_ohlcv
# ---------------------------------------------------------------------------

class TestFetchOhlcv:
    def test_success_returns_data(self, wrapper):
        expected = [[1000, 50000, 51000, 49000, 50500, 100]]
        wrapper.exchange.fetch_ohlcv.return_value = expected

        result = wrapper.fetch_ohlcv("BTC/USDC", "1h", limit=1)

        assert result == expected
        wrapper.exchange.fetch_ohlcv.assert_called_once()

    def test_caches_on_success(self, wrapper):
        wrapper.exchange.fetch_ohlcv.return_value = [[1, 2, 3, 4, 5, 6]]
        wrapper.fetch_ohlcv("ETH/USDC", "4h")

        key = wrapper._cache_key("ohlcv", "ETH/USDC", "4h", 500)
        assert key in wrapper._cache

    def test_returns_cache_on_network_error(self, wrapper):
        cached = [[9, 8, 7, 6, 5, 4]]
        wrapper._set_cache(wrapper._cache_key("ohlcv", "BTC/USDC", "1h", 500), cached)
        wrapper.exchange.fetch_ohlcv.side_effect = ccxt.NetworkError("timeout")

        result = wrapper.fetch_ohlcv("BTC/USDC", "1h")
        assert result == cached

    def test_raises_when_no_cache_on_network_error(self, wrapper):
        wrapper.exchange.fetch_ohlcv.side_effect = ccxt.NetworkError("down")
        with pytest.raises(ccxt.NetworkError):
            wrapper.fetch_ohlcv("XRP/USDC", "1d")


# ---------------------------------------------------------------------------
# execute_order
# ---------------------------------------------------------------------------

class TestExecuteOrder:
    def test_market_buy_success(self, wrapper):
        order = {"id": "123", "status": "closed"}
        wrapper.exchange.create_market_order.return_value = order

        result = wrapper.execute_order("BTC/USDC", "buy", 0.001)
        assert result == order

    def test_limit_sell_success(self, wrapper):
        order = {"id": "456", "status": "open"}
        wrapper.exchange.create_limit_order.return_value = order

        result = wrapper.execute_order("BTC/USDC", "sell", 0.001, price=50000, order_type="limit")
        assert result == order

    def test_returns_none_on_insufficient_funds(self, wrapper):
        wrapper.exchange.create_market_order.side_effect = ccxt.InsufficientFunds("no funds")
        result = wrapper.execute_order("BTC/USDC", "buy", 999)
        assert result is None

    def test_returns_none_on_invalid_order(self, wrapper):
        wrapper.exchange.create_market_order.side_effect = ccxt.InvalidOrder("bad order")
        result = wrapper.execute_order("BTC/USDC", "buy", 0.0001)
        assert result is None

    def test_retry_on_rate_limit(self, wrapper):
        wrapper._max_attempts = 2
        wrapper._rate_limit_wait = 0
        order = {"id": "789"}
        wrapper.exchange.create_market_order.side_effect = [
            ccxt.RateLimitExceeded("limit"),
            order,
        ]
        result = wrapper.execute_order("BTC/USDC", "buy", 0.001)
        assert result == order


# ---------------------------------------------------------------------------
# cancel_order
# ---------------------------------------------------------------------------

class TestCancelOrder:
    def test_cancel_success(self, wrapper):
        wrapper.exchange.cancel_order.return_value = {}
        assert wrapper.cancel_order("ord_1", "BTC/USDC") is True

    def test_cancel_not_found_returns_false(self, wrapper):
        wrapper.exchange.cancel_order.side_effect = ccxt.OrderNotFound("gone")
        assert wrapper.cancel_order("ord_x", "BTC/USDC") is False


# ---------------------------------------------------------------------------
# health_check
# ---------------------------------------------------------------------------

class TestHealthCheck:
    def test_healthy_when_exchange_reachable(self, wrapper):
        wrapper.exchange.fetch_time.return_value = 1_000_000
        result = wrapper.health_check()
        assert result["status"] == "healthy"
        assert wrapper.health_status["binance"] is True

    def test_critical_when_exchange_down(self, wrapper):
        wrapper.exchange.fetch_time.side_effect = ccxt.NetworkError("down")
        result = wrapper.health_check()
        assert result["status"] == "critical"
        assert wrapper.health_status["binance"] is False


# ---------------------------------------------------------------------------
# Global exception handler
# ---------------------------------------------------------------------------

class TestGlobalExceptionHandler:
    def test_installs_without_error(self):
        setup_global_exception_handler()
        import sys
        assert sys.excepthook is not None
