"""Telegram Bot — interactive notifications and manual trade approvals.

Provides:
  - Screener proposal with inline keyboard (✅ AL / ❌ REDDET / 📊 DETAY)
  - Trade execution notifications
  - Daily P&L report
  - Manual commands: /status, /pnl, /sat, /grid, /screener
  - Fire-and-forget ``send_alert_sync`` for use by other modules
"""

import asyncio
import json
import logging
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import TypedDict

import yaml
from dotenv import load_dotenv
from telegram import Bot, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

load_dotenv(Path(__file__).parent.parent / ".env")

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level bot instance (shared)
# ---------------------------------------------------------------------------

_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
_chat_id: str = os.getenv("TELEGRAM_CHAT_ID", "")
_bot: Bot | None = None
_loop: asyncio.AbstractEventLoop | None = None
_loop_thread: threading.Thread | None = None


def _get_bot() -> Bot:
    global _bot
    if _bot is None:
        _bot = Bot(token=_bot_token)
    return _bot


def _get_loop() -> asyncio.AbstractEventLoop | None:
    """Return the running event loop if available, None otherwise.

    Avoids creating new loops - uses the main loop from the orchestrator.
    """
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


# ---------------------------------------------------------------------------
# TypedDicts
# ---------------------------------------------------------------------------

class TradeNotification(TypedDict):
    """Data required for a trade execution notification."""

    pair: str
    side: str            # 'buy' | 'sell'
    amount: float
    price: float
    cost: float
    fee: float
    entry_price: float | None   # None for buy notifications
    hold_time_hours: float | None


# ---------------------------------------------------------------------------
# Fire-and-forget alert (used by all other modules)
# ---------------------------------------------------------------------------

def send_alert_sync(message: str) -> None:
    """Send a Telegram message from any sync context (thread-safe).

    This is the primary function used by other modules to fire alerts
    without awaiting a coroutine. Uses the running event loop if available,
    otherwise creates a temporary one.

    Args:
        message: Plain-text or HTML message to send.
    """
    if not _bot_token or not _chat_id:
        logger.warning("Telegram not configured — alert skipped")
        return

    async def _send():
        try:
            await _send_message(message)
        except Exception as exc:
            logger.error(f"Failed to send Telegram alert: {exc}")

    try:
        # Try to get the running loop
        loop = asyncio.get_running_loop()
        # We're in an async context, create task
        asyncio.create_task(_send())
    except RuntimeError:
        # No running loop, use a temporary one
        try:
            asyncio.run(_send())
        except Exception as exc:
            logger.error(f"send_alert_sync failed: {exc}")


async def _send_message(text: str, chat_id: str | None = None, **kwargs) -> None:
    """Internal coroutine to send a Telegram message."""
    bot = _get_bot()
    await bot.send_message(
        chat_id=chat_id or _chat_id,
        text=text,
        parse_mode="HTML",
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Screener proposal
# ---------------------------------------------------------------------------

async def send_screener_proposal(
    candidate: dict,
    position_size: float,
    on_buy_callback,
    on_reject_callback,
) -> None:
    """Send an interactive screener proposal with inline keyboard.

    Args:
        candidate: ScreenerCandidate dict (multi-TF fields supported).
        position_size: Suggested USDC amount.
        on_buy_callback: Coroutine to call when user taps ✅ AL.
        on_reject_callback: Coroutine to call when user taps ❌ REDDET.
    """
    pair = candidate["pair"]
    price = candidate["price"]
    volume = candidate["volume"]
    score = candidate["score"]

    # Multi-TF alanlar (eski 'ema200' alanı da geriye uyumlu çalışır)
    rsi_1h = candidate.get("rsi_1h", 50.0)
    rsi_4h = candidate.get("rsi_4h", 50.0)
    rsi_1d = candidate.get("rsi_1d", 50.0)
    ema200_1h = candidate.get("ema200_1h", candidate.get("ema200", 0))
    ema200_4h = candidate.get("ema200_4h", candidate.get("ema200", 0))
    ema200_1d = candidate.get("ema200_1d", candidate.get("ema200", 0))
    dist_1h = candidate.get("distance_pct_1h", candidate.get("distance_pct", 0))
    dist_4h = candidate.get("distance_pct_4h", candidate.get("distance_pct", 0))
    dist_1d = candidate.get("distance_pct_1d", candidate.get("distance_pct", 0))
    signal_strength = candidate.get("signal_strength", "—")
    buy_suggestion = candidate.get("buy_suggestion", "İZLE")

    def _rsi_icon(rsi: float, thr: float) -> str:
        return "🔴" if rsi < thr else ("🟡" if rsi < thr + 10 else "🟢")

    stop = price * 0.95
    ladder = [price * 1.15, price * 1.18, price * 1.20]
    ema_gain_1d = ((ema200_1d / price) - 1) * 100 if ema200_1d > 0 else 0.0

    msg = (
        f"🔍 <b>YENİ FIRSAT BULUNDU!</b>\n\n"
        f"📌 <b>{pair}</b>\n"
        f"💰 Önerilen: <b>{position_size:.0f} USDC</b>\n"
        f"⚡ Sinyal: <b>{signal_strength}</b>  →  <b>{buy_suggestion}</b>\n\n"
        f"📊 <b>MULTI-TIMEFRAME ANALİZ:</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"1H RSI: {rsi_1h:5.1f} {_rsi_icon(rsi_1h, 40)}  "
        f"EMA200: ${ema200_1h:.4f}  (-{dist_1h:.1f}%)\n"
        f"4H RSI: {rsi_4h:5.1f} {_rsi_icon(rsi_4h, 35)}  "
        f"EMA200: ${ema200_4h:.4f}  (-{dist_4h:.1f}%)\n"
        f"1D RSI: {rsi_1d:5.1f} {_rsi_icon(rsi_1d, 30)}  "
        f"EMA200: ${ema200_1d:.4f}  (-{dist_1d:.1f}%)\n\n"
        f"💵 Fiyat: ${price:.6f}\n"
        f"📦 Hacim: ${volume/1_000_000:.1f}M USDC\n\n"
        f"🎯 <b>ÇIKIŞ PLANI (Hybrid):</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Entry : ${price:.6f}\n"
        f"Stop  : ${stop:.6f}  (-5%)\n\n"
        f"EMA200(1D) Touch → %40 sat (+{ema_gain_1d:.1f}%)\n"
        f"• %30 @ +15%  →  ${ladder[0]:.6f}\n"
        f"• %20 @ +18%  →  ${ladder[1]:.6f}\n"
        f"• %10 @ +20%  →  ${ladder[2]:.6f}\n\n"
        f"⚡ <b>FIRSAT SKORU: {score}/120</b>"
    )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "✅ AL", callback_data=f"buy_{pair}_{position_size}"
            ),
            InlineKeyboardButton(
                "❌ REDDET", callback_data=f"reject_{pair}"
            ),
        ],
        [InlineKeyboardButton("📊 DETAY", callback_data=f"detail_{pair}")],
    ])

    bot = _get_bot()
    await bot.send_message(
        chat_id=_chat_id,
        text=msg,
        parse_mode="HTML",
        reply_markup=keyboard,
    )
    logger.info(f"Screener proposal sent: {pair}")

    # 24-hour timeout
    await asyncio.sleep(86400)
    await _send_message(f"⏰ {pair} önerisi timeout oldu (24h geçti)")


# ---------------------------------------------------------------------------
# Trade notification
# ---------------------------------------------------------------------------

async def notify_trade_execution(trade: TradeNotification) -> None:
    """Send a trade execution notification.

    Args:
        trade: TradeNotification with side, pair, price, amount, etc.
    """
    if trade["side"] == "buy":
        emoji, action = "🟢", "ALIŞ"
    else:
        emoji, action = "🔴", "SATIŞ"

    msg = (
        f"{emoji} <b>{action}: {trade['pair']}</b>\n"
        f"Miktar: {trade['amount']:.6f}\n"
        f"Fiyat: ${trade['price']:.6f}\n"
        f"Toplam: {trade['cost']:.2f} USDC\n"
        f"Fee: {trade['fee']:.6f} USDC"
    )

    if trade["side"] == "sell" and trade.get("entry_price"):
        entry = trade["entry_price"]
        pnl_pct = ((trade["price"] - entry) / entry) * 100
        pnl_usdc = (trade["price"] - entry) * trade["amount"]
        hours = trade.get("hold_time_hours") or 0
        msg += (
            f"\n━━━━━━━━━━━━━━━━━\n"
            f"Entry: ${entry:.6f}\n"
            f"P&L: {pnl_usdc:+.2f} USDC ({pnl_pct:+.2f}%)\n"
            f"Hold: {hours:.1f} saat"
        )

    await _send_message(msg)
    logger.info(f"Trade notification sent: {trade['side']} {trade['pair']}")


# ---------------------------------------------------------------------------
# Daily report
# ---------------------------------------------------------------------------

async def send_daily_report(stats: dict) -> None:
    """Send a formatted daily P&L and portfolio report.

    Args:
        stats: Dict produced by ``main.calculate_daily_stats()``.
    """
    msg = (
        f"📊 <b>GÜNLÜK ÖZET</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🗓️ {datetime.now().strftime('%d %B %Y')}\n\n"
        f"💰 <b>P&L:</b>\n"
        f"├─ Günlük:  {stats.get('daily_pnl', 0):+.2f} USDC "
        f"({stats.get('daily_pnl_pct', 0):+.2f}%)\n"
        f"├─ Haftalık: {stats.get('weekly_pnl', 0):+.2f} USDC\n"
        f"├─ Aylık:    {stats.get('monthly_pnl', 0):+.2f} USDC\n"
        f"└─ Toplam:   {stats.get('total_pnl', 0):+.2f} USDC\n\n"
        f"📈 <b>İSTATİSTİKLER:</b>\n"
        f"├─ Toplam: {stats.get('total_trades', 0)} trade\n"
        f"├─ Karlı:  {stats.get('winning_trades', 0)} "
        f"({stats.get('win_rate', 0):.1f}%)\n"
        f"└─ Zararlı: {stats.get('losing_trades', 0)}\n\n"
        f"💵 <b>BAKİYE:</b>\n"
        f"├─ Total:     {stats.get('total_balance', 0):.2f} USDC\n"
        f"├─ Grid:      {stats.get('grid_locked', 0):.2f} USDC\n"
        f"├─ Screener:  {stats.get('screener_locked', 0):.2f} USDC\n"
        f"└─ Kullanılabilir: {stats.get('available', 0):.2f} USDC ✅"
    )
    await _send_message(msg)
    logger.info("Daily report sent")


# ---------------------------------------------------------------------------
# Telegram Application (command handlers)
# ---------------------------------------------------------------------------

_STATUS_FILE = Path(__file__).parent.parent / "logs" / "status.json"


class TelegramBotApp:
    """Full Telegram bot application with command handlers.

    Commands:
        /start    — Welcome message + command list
        /status   — Portfolio overview
        /health   — Binance API + bot health check
        /report   — Full status.json dump (copy-paste for AI analysis)
        /pnl      — P&L breakdown
        /sat      — Manual sell (e.g. /sat MATIC +20 or /sat MATIC market)
        /grid     — Grid positions detail
        /screener — Trigger manual screener run
    """

    def __init__(self, main_orchestrator=None) -> None:
        """Initialise bot application.

        Args:
            main_orchestrator: Reference to main.BotOrchestrator for callbacks.
        """
        self._orchestrator = main_orchestrator
        self._app: Application | None = None
        logger.info("TelegramBotApp initialised")

    # Bot command menu shown in Telegram's "/" menu
    _BOT_COMMANDS = [
        BotCommand("start",    "Bot durumu ve komut listesi"),
        BotCommand("status",   "Portfolio bakiye durumu"),
        BotCommand("health",   "Binance API + bot saglik raporu"),
        BotCommand("report",   "Tam durum raporu (AI analizi icin)"),
        BotCommand("pnl",      "P&L raporu"),
        BotCommand("grid",     "Grid pozisyonlari"),
        BotCommand("screener", "Manuel screener calistir"),
        BotCommand("sat",      "Manuel satis: /sat MATIC market"),
    ]

    def build(self) -> Application:
        """Build and configure the Telegram Application."""
        self._app = (
            Application.builder()
            .token(_bot_token)
            .post_init(self._on_startup)
            .build()
        )
        self._app.add_handler(CommandHandler("start", self._cmd_start))
        self._app.add_handler(CommandHandler("status", self._cmd_status))
        self._app.add_handler(CommandHandler("health", self._cmd_health))
        self._app.add_handler(CommandHandler("report", self._cmd_report))
        self._app.add_handler(CommandHandler("pnl", self._cmd_pnl))
        self._app.add_handler(CommandHandler("sat", self._cmd_sell))
        self._app.add_handler(CommandHandler("grid", self._cmd_grid))
        self._app.add_handler(CommandHandler("screener", self._cmd_screener))
        self._app.add_handler(CallbackQueryHandler(self._handle_callback))
        logger.info("Telegram handlers registered: start, status, health, report, pnl, sat, grid, screener")
        return self._app

    async def _on_startup(self, app: Application) -> None:
        """Register bot command menu with Telegram on startup."""
        try:
            await app.bot.set_my_commands(self._BOT_COMMANDS)
            logger.info("Telegram command menu registered (%d commands)", len(self._BOT_COMMANDS))
        except Exception as exc:
            logger.warning(f"Could not set Telegram command menu: {exc}")

    async def start_polling(self) -> None:
        """Start bot in long-polling mode with proper error handling.

        Uses initialize() + start() for non-blocking operation within the
        existing event loop, avoiding conflicts with send_alert_sync.
        """
        app = self.build()

        # Add error handler to catch and log errors without crashing
        app.add_error_handler(self._error_handler)

        try:
            # Use initialize + start instead of run_polling for better control
            await app.initialize()
            await app.start()
            await app.updater.start_polling(drop_pending_updates=True)
            logger.info("Telegram bot polling started successfully")

            # Keep the coroutine alive
            while True:
                await asyncio.sleep(3600)  # Sleep for an hour
        except asyncio.CancelledError:
            logger.info("Telegram polling cancelled")
            raise
        except Exception as exc:
            logger.error(f"Telegram polling error: {exc}")
            raise

    async def _error_handler(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle errors in the telegram bot.

        Logs the error and prevents the bot from crashing on non-critical errors.
        """
        logger.error(f"Telegram error: {context.error}", exc_info=context.error)

        # Don't crash on Conflict errors - they resolve themselves
        if isinstance(context.error, Exception):
            error_str = str(context.error)
            if "Conflict" in error_str:
                logger.warning("Telegram Conflict detected - another instance may be running")
                return  # Don't propagate, let it retry

    # ------------------------------------------------------------------
    # Command handlers
    # ------------------------------------------------------------------

    async def _cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text(
            "🤖 <b>Akıllı Grid Trading Bot</b> çevrimiçi!\n\n"
            "📋 <b>Komutlar:</b>\n"
            "/status   — Portfolio bakiye durumu\n"
            "/health   — Binance API + bot sağlık raporu\n"
            "/report   — Tam durum raporu (AI analizi için)\n"
            "/pnl      — P&L raporu\n"
            "/grid     — Grid pozisyonları\n"
            "/screener — Manuel screener çalıştır\n"
            "/sat PAIR AMOUNT — Manuel satış\n\n"
            "💡 Sorun varsa /report çıktısını AI'a yapıştır.",
            parse_mode="HTML",
        )

    async def _cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show portfolio status."""
        try:
            if self._orchestrator:
                snap = self._orchestrator.capital_manager.get_balance_snapshot()
                msg = (
                    f"📊 <b>PORTFOLIO DURUMU</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"Total:     {snap['total']:.2f} USDC\n"
                    f"Grid:      {snap['grid_locked']:.2f} USDC\n"
                    f"Screener:  {snap['screener_locked']:.2f} USDC\n"
                    f"Mevcut:    {snap['available']:.2f} USDC ✅"
                )
            else:
                msg = "⚠️ Orchestrator bağlantısı yok"
            await update.message.reply_text(msg, parse_mode="HTML")
        except Exception as exc:
            await update.message.reply_text(f"❌ Hata: {exc}")

    async def _cmd_health(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show Binance API + bot health status."""
        await update.message.reply_text("🔍 Sağlık kontrolü yapılıyor …")
        try:
            # Exchange ping
            if self._orchestrator:
                result = await asyncio.to_thread(self._orchestrator.exchange.health_check)
                risk = self._orchestrator.risk_manager.health_check()
                snap = self._orchestrator.capital_manager.get_balance_snapshot()

                exchange_icon = "🟢" if result["status"] == "healthy" else "🔴"
                risk_icon = {"healthy": "🟢", "degraded": "🟡", "critical": "🔴"}.get(
                    risk["status"], "⚪"
                )

                msg = (
                    f"🏥 <b>SAĞLIK RAPORU</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"{exchange_icon} Binance API: <b>{result['status']}</b>\n"
                    f"   Gecikme: {result.get('latency_ms', '?')} ms\n\n"
                    f"{risk_icon} Risk Durumu: <b>{risk['status']}</b>\n"
                    f"   Circuit Breaker: {'🔴 AKTİF' if risk['circuit_breaker'] else '🟢 Kapalı'}\n"
                    f"   Ardışık Kayıp: {risk['consecutive_losses']}\n"
                    f"   Günlük P&L: {risk['daily_pnl_pct']:+.2f}%\n\n"
                    f"💰 <b>Bakiye:</b>\n"
                    f"   Total: {snap['total']:.2f} USDC\n"
                    f"   Mevcut: {snap['available']:.2f} USDC\n\n"
                    f"🕐 {datetime.now().strftime('%H:%M:%S UTC')}"
                )
            else:
                # Standalone: read status.json
                if _STATUS_FILE.exists():
                    data = json.loads(_STATUS_FILE.read_text())
                    msg = (
                        f"🏥 <b>SAĞLIK RAPORU</b> (status.json)\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"Bot: {data.get('bot_status', '?')}\n"
                        f"Exchange: {data.get('exchange', {}).get('status', '?')}\n"
                        f"Zaman: {data.get('timestamp', '?')}"
                    )
                else:
                    msg = "⚠️ Orchestrator bağlantısı yok ve status.json bulunamadı"
            await update.message.reply_text(msg, parse_mode="HTML")
        except Exception as exc:
            await update.message.reply_text(f"❌ Sağlık kontrolü hatası: {exc}")
            logger.error(f"_cmd_health error: {exc}")

    async def _cmd_report(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Send full status.json — copy-paste this to AI for analysis."""
        try:
            if _STATUS_FILE.exists():
                data = json.loads(_STATUS_FILE.read_text())
                report_text = json.dumps(data, indent=2, ensure_ascii=False)
                # Telegram message limit is 4096 chars
                if len(report_text) > 3800:
                    report_text = report_text[:3800] + "\n... (truncated)"
                msg = (
                    f"📋 <b>TAM DURUM RAPORU</b>\n"
                    f"Bu çıktıyı AI'a yapıştırabilirsin:\n\n"
                    f"<pre>{report_text}</pre>"
                )
                await update.message.reply_text(msg, parse_mode="HTML")
            else:
                await update.message.reply_text(
                    "⚠️ logs/status.json henüz oluşturulmadı.\n"
                    "Bot başlatıldıktan 30 saniye sonra tekrar dene."
                )
        except Exception as exc:
            await update.message.reply_text(f"❌ Rapor hatası: {exc}")
            logger.error(f"_cmd_report error: {exc}")

    async def _cmd_pnl(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show P&L breakdown."""
        await update.message.reply_text("📈 P&L raporu hazırlanıyor …")

    async def _cmd_sell(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Manual sell command: /sat PAIR AMOUNT or /sat PAIR market."""
        args = context.args or []
        if len(args) < 2:
            await update.message.reply_text(
                "Kullanım: /sat MATIC +20 veya /sat MATIC market"
            )
            return

        pair = args[0].upper() + "/USDC"
        amount_arg = args[1].lower()

        await update.message.reply_text(f"⏳ {pair} için satış emri hazırlanıyor …")

        try:
            if self._orchestrator:
                if amount_arg == "market":
                    order = self._orchestrator.exchange.execute_order(
                        pair, "sell", 0, order_type="market"
                    )
                    await update.message.reply_text(f"✅ Market satış: {pair}")
                else:
                    profit_pct = float(amount_arg.replace("+", "")) / 100
                    await update.message.reply_text(
                        f"✅ Limit satış emri: {pair} +{profit_pct*100:.1f}%"
                    )
            else:
                await update.message.reply_text("⚠️ Orchestrator bağlantısı yok")
        except Exception as exc:
            await update.message.reply_text(f"❌ Satış hatası: {exc}")

    async def _cmd_grid(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show grid positions."""
        await update.message.reply_text("📊 Grid pozisyonları yükleniyor …")

    async def _cmd_screener(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Trigger manual screener run."""
        await update.message.reply_text("🔍 Screener başlatılıyor …")
        if self._orchestrator:
            try:
                candidates = await asyncio.to_thread(
                    self._orchestrator.screener.daily_screener
                )
                if candidates:
                    msg = f"✅ {len(candidates)} aday bulundu:\n"
                    for c in candidates:
                        msg += f"• {c['pair']}: skor={c['score']}\n"
                else:
                    msg = "❌ Uygun aday bulunamadı"
                await update.message.reply_text(msg)
            except Exception as exc:
                await update.message.reply_text(f"❌ Screener hatası: {exc}")

    # ------------------------------------------------------------------
    # Callback query handler (inline keyboard)
    # ------------------------------------------------------------------

    async def _handle_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle inline keyboard button presses."""
        query = update.callback_query
        await query.answer()
        data = query.data

        if data.startswith("buy_"):
            parts = data.split("_")
            pair = parts[1]
            amount = float(parts[2]) if len(parts) > 2 else 0
            await self._handle_buy(query, pair, amount)

        elif data.startswith("reject_"):
            pair = data.replace("reject_", "")
            await query.edit_message_text(f"❌ {pair} önerisi reddedildi")
            logger.info(f"Screener proposal rejected: {pair}")

        elif data.startswith("detail_"):
            pair = data.replace("detail_", "")
            await query.edit_message_text(
                f"📊 {pair} detayı için screener sonuçlarına bakın"
            )

    async def _handle_buy(self, query, pair: str, amount: float) -> None:
        """Execute buy after manual approval."""
        await query.edit_message_text(f"⏳ {pair} alımı gerçekleştiriliyor …")
        try:
            if self._orchestrator:
                available = self._orchestrator.capital_manager.check_available_balance()
                if available < amount:
                    await query.edit_message_text(
                        f"⚠️ Yetersiz bakiye!\n"
                        f"Gerekli: {amount} USDC\n"
                        f"Mevcut: {available:.2f} USDC\n\n"
                        f"Sıra bekleniyor …"
                    )
                    self._orchestrator.capital_manager.add_to_pending_queue(
                        pair, amount, 0
                    )
                    return

                order = self._orchestrator.exchange.execute_order(
                    pair, "buy", amount / 1, order_type="market"  # amount/price = qty
                )
                if order:
                    await query.edit_message_text(
                        f"✅ <b>ALIM GERÇEKLEŞTİ</b>\n\n"
                        f"{pair}\n"
                        f"Toplam: {amount} USDC",
                        parse_mode="HTML",
                    )
                else:
                    await query.edit_message_text(f"❌ {pair} alımı başarısız")
            else:
                await query.edit_message_text("⚠️ Orchestrator bağlantısı yok")
        except Exception as exc:
            await query.edit_message_text(f"❌ Hata: {exc}")
            logger.error(f"Buy callback error: {exc}")
