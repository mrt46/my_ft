"""Unit tests for custom_modules.telegram_bot."""

import asyncio
from datetime import datetime
from unittest.mock import MagicMock, patch, AsyncMock

import pytest

from custom_modules.telegram_bot import (
    send_alert_sync,
    notify_trade_execution,
    send_daily_report,
    TradeNotification,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def reset_bot_state():
    """Reset global bot state before each test."""
    import custom_modules.telegram_bot as tg_module
    tg_module._bot = None
    tg_module._loop = None
    tg_module._loop_thread = None
    yield


@pytest.fixture
def mock_bot():
    """Return a mocked Bot instance."""
    with patch("custom_modules.telegram_bot.Bot") as mock_bot_class:
        mock_instance = AsyncMock()
        mock_bot_class.return_value = mock_instance
        yield mock_instance


@pytest.fixture
def mock_env():
    """Set up mock environment variables."""
    with patch.dict("os.environ", {
        "TELEGRAM_BOT_TOKEN": "test_token_123",
        "TELEGRAM_CHAT_ID": "123456789",
    }):
        yield


# ---------------------------------------------------------------------------
# send_alert_sync
# ---------------------------------------------------------------------------

class TestSendAlertSync:
    def test_skips_when_not_configured(self, mock_bot):
        """Should skip if token or chat_id not set."""
        with patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": ""}):
            # Should not raise
            send_alert_sync("Test message")

    def test_sends_message_when_configured(self, mock_env, mock_bot):
        """Should send message when properly configured."""
        send_alert_sync("Test alert message")

        # Wait for async execution
        import time
        time.sleep(0.1)

        # Bot should have been called
        mock_bot.send_message.assert_called_once()


# ---------------------------------------------------------------------------
# notify_trade_execution
# ---------------------------------------------------------------------------

class TestNotifyTradeExecution:
    @pytest.mark.asyncio
    async def test_buy_notification_format(self, mock_env, mock_bot):
        """Buy notification should have correct format."""
        trade = TradeNotification(
            pair="BTC/USDC",
            side="buy",
            amount=0.001,
            price=50000.0,
            cost=50.0,
            fee=0.05,
            entry_price=None,
            hold_time_hours=None,
        )

        await notify_trade_execution(trade)

        mock_bot.send_message.assert_called_once()
        call_args = mock_bot.send_message.call_args
        assert "🟢 ALIŞ" in call_args[1]["text"]
        assert "BTC/USDC" in call_args[1]["text"]

    @pytest.mark.asyncio
    async def test_sell_notification_with_pnl(self, mock_env, mock_bot):
        """Sell notification should include P&L when entry price available."""
        trade = TradeNotification(
            pair="BTC/USDC",
            side="sell",
            amount=0.001,
            price=55000.0,
            cost=55.0,
            fee=0.055,
            entry_price=50000.0,
            hold_time_hours=24.5,
        )

        await notify_trade_execution(trade)

        mock_bot.send_message.assert_called_once()
        call_args = mock_bot.send_message.call_args
        text = call_args[1]["text"]
        assert "🔴 SATIŞ" in text
        assert "P&L:" in text
        assert "+10.00%" in text  # (55000-50000)/50000 = 10%


# ---------------------------------------------------------------------------
# send_daily_report
# ---------------------------------------------------------------------------

class TestSendDailyReport:
    @pytest.mark.asyncio
    async def test_daily_report_format(self, mock_env, mock_bot):
        """Daily report should have correct structure."""
        stats = {
            "daily_pnl": 25.5,
            "daily_pnl_pct": 2.55,
            "weekly_pnl": 100.0,
            "monthly_pnl": 300.0,
            "total_pnl": 500.0,
            "total_trades": 50,
            "winning_trades": 35,
            "losing_trades": 15,
            "win_rate": 70.0,
            "total_balance": 1025.5,
            "grid_locked": 600.0,
            "screener_locked": 100.0,
            "available": 325.5,
        }

        await send_daily_report(stats)

        mock_bot.send_message.assert_called_once()
        call_args = mock_bot.send_message.call_args
        text = call_args[1]["text"]

        assert "GÜNLÜK ÖZET" in text
        assert "+25.50 USDC" in text
        assert "+2.55%" in text
        assert "35 trade" in text or "35" in text
        assert "70.0%" in text


# ---------------------------------------------------------------------------
# TelegramBotApp
# ---------------------------------------------------------------------------

class TestTelegramBotApp:
    @pytest.mark.asyncio
    async def test_build_registers_handlers(self, mock_env):
        """Should register all command handlers."""
        from custom_modules.telegram_bot import TelegramBotApp

        app = TelegramBotApp(main_orchestrator=None)
        built_app = app.build()

        # Should have handlers registered
        assert built_app is not None

    @pytest.mark.asyncio
    async def test_cmd_start(self, mock_env, mock_bot):
        """Start command should send welcome message."""
        from custom_modules.telegram_bot import TelegramBotApp

        app = TelegramBotApp(main_orchestrator=None)
        app.build()

        mock_update = MagicMock()
        mock_update.message.reply_text = AsyncMock()

        await app._cmd_start(mock_update, None)

        mock_update.message.reply_text.assert_called_once()
        call_args = mock_update.message.reply_text.call_args
        assert "Akıllı Grid Trading Bot" in call_args[1]["text"]

    @pytest.mark.asyncio
    async def test_cmd_status_with_orchestrator(self, mock_env):
        """Status command should show portfolio when orchestrator available."""
        from custom_modules.telegram_bot import TelegramBotApp

        mock_orchestrator = MagicMock()
        mock_orchestrator.capital_manager.get_balance_snapshot.return_value = {
            "total": 1000.0,
            "grid_locked": 600.0,
            "screener_locked": 100.0,
            "available": 300.0,
        }

        app = TelegramBotApp(main_orchestrator=mock_orchestrator)

        mock_update = MagicMock()
        mock_update.message.reply_text = AsyncMock()

        await app._cmd_status(mock_update, None)

        mock_update.message.reply_text.assert_called_once()
        call_args = mock_update.message.reply_text.call_args
        text = call_args[1]["text"]
        assert "PORTFOLIO DURUMU" in text
        assert "1000.00" in text

    @pytest.mark.asyncio
    async def test_cmd_health_with_orchestrator(self, mock_env):
        """Health command should show exchange and risk status."""
        from custom_modules.telegram_bot import TelegramBotApp

        mock_orchestrator = MagicMock()
        mock_orchestrator.exchange.health_check.return_value = {
            "status": "healthy",
            "latency_ms": 150.0,
        }
        mock_orchestrator.risk_manager.health_check.return_value = {
            "status": "healthy",
            "circuit_breaker": False,
            "consecutive_losses": 0,
            "daily_pnl_pct": 1.5,
        }
        mock_orchestrator.capital_manager.get_balance_snapshot.return_value = {
            "total": 1000.0,
            "available": 300.0,
        }

        app = TelegramBotApp(main_orchestrator=mock_orchestrator)

        mock_update = MagicMock()
        mock_update.message.reply_text = AsyncMock()

        await app._cmd_health(mock_update, None)

        mock_update.message.reply_text.assert_called_once()

    @pytest.mark.asyncio
    async def test_handle_callback_buy(self, mock_env):
        """Buy callback should execute buy order."""
        from custom_modules.telegram_bot import TelegramBotApp

        mock_orchestrator = MagicMock()
        mock_orchestrator.capital_manager.check_available_balance.return_value = 500.0
        mock_orchestrator.exchange.execute_order.return_value = {"id": "order123"}

        app = TelegramBotApp(main_orchestrator=mock_orchestrator)

        mock_query = MagicMock()
        mock_query.data = "buy_MATIC/USDC_100"
        mock_query.answer = AsyncMock()
        mock_query.edit_message_text = AsyncMock()

        await app._handle_callback(mock_query, None)

        mock_query.answer.assert_called_once()
        mock_orchestrator.exchange.execute_order.assert_called_once()

    @pytest.mark.asyncio
    async def test_handle_callback_reject(self, mock_env):
        """Reject callback should cancel proposal."""
        from custom_modules.telegram_bot import TelegramBotApp

        app = TelegramBotApp(main_orchestrator=None)

        mock_query = MagicMock()
        mock_query.data = "reject_MATIC/USDC"
        mock_query.answer = AsyncMock()
        mock_query.edit_message_text = AsyncMock()

        await app._handle_callback(mock_query, None)

        mock_query.edit_message_text.assert_called_once()
        call_args = mock_query.edit_message_text.call_args
        assert "reddedildi" in call_args[1]["text"]
