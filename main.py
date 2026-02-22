"""main.py — Akıllı Grid Trading Bot Orchestrator.

Entry point for the trading bot. Initialises all modules, starts
schedulers, launches the Telegram bot, and coordinates the 2-hour
analysis cycle, daily screener, EMA updates, and health checks.

Usage:
    python main.py             # Production
    python main.py --dry-run   # Paper trading (overrides settings.yaml)
"""

import argparse
import asyncio
import json
import logging
import logging.handlers
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml

# ---------------------------------------------------------------------------
# Logging setup (must happen before other imports)
# ---------------------------------------------------------------------------

def setup_logging(log_level: str = "INFO") -> None:
    """Configure file + console logging with rotation.

    Args:
        log_level: Logging level string (INFO, DEBUG, WARNING, …).
    """
    log_dir = Path(__file__).parent / "logs"
    log_dir.mkdir(exist_ok=True)

    settings_path = Path(__file__).parent / "config" / "settings.yaml"
    with open(settings_path) as fh:
        cfg = yaml.safe_load(fh)

    log_cfg = cfg.get("logging", {})
    fmt = log_cfg.get("format", "%(asctime)s [%(levelname)-8s] %(name)s: %(message)s")
    datefmt = log_cfg.get("date_format", "%Y-%m-%d %H:%M:%S")
    max_bytes = log_cfg.get("max_bytes", 52_428_800)
    backup_count = log_cfg.get("backup_count", 7)

    formatter = logging.Formatter(fmt, datefmt=datefmt)

    handlers: list[logging.Handler] = [
        logging.StreamHandler(sys.stdout),
        logging.handlers.RotatingFileHandler(
            log_dir / "trades.log", maxBytes=max_bytes, backupCount=backup_count
        ),
        logging.handlers.RotatingFileHandler(
            log_dir / "api_errors.log", maxBytes=max_bytes, backupCount=backup_count,
            delay=True,
        ),
    ]
    for h in handlers:
        h.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(getattr(logging, log_level.upper(), logging.INFO))
    # Remove any existing handlers to prevent duplicate log lines when
    # setup_logging() is called more than once (e.g. CLI re-invocation).
    for existing in list(root.handlers):
        root.removeHandler(existing)
    for h in handlers:
        root.addHandler(h)


setup_logging()
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module imports (after logging is ready)
# ---------------------------------------------------------------------------

from custom_modules.api_wrapper import ResilientExchangeWrapper, setup_global_exception_handler
from custom_modules.capital_manager import CapitalManager
from custom_modules.bnb_manager import BnbManager
from custom_modules.grid_analyzer import GridAnalyzer
from custom_modules.sentiment_analyzer import SentimentAnalyzer
from custom_modules.grid_fusion import GridFusion
from custom_modules.screener import Screener
from custom_modules.hybrid_exit import HybridExitManager
from custom_modules.telegram_bot import TelegramBotApp, send_alert_sync, send_daily_report
from custom_modules.risk_manager import RiskManager


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

class BotOrchestrator:
    """Top-level orchestrator for all trading bot modules.

    Coordinates:
        - 2-hour grid analysis cycle
        - Daily 00:00 UTC screener run
        - Every-4-hour EMA update for screener exits
        - Every-30-second health check
        - Telegram bot (polling in background)

    Attributes:
        exchange: ResilientExchangeWrapper instance.
        capital_manager: CapitalManager instance.
        risk_manager: RiskManager instance.
        screener: Screener instance.
        exit_manager: HybridExitManager instance.
    """

    def __init__(self, dry_run: bool | None = None) -> None:
        """Initialise all modules.

        Args:
            dry_run: Override settings.yaml dry_run flag. If None, uses
                     the value from settings.yaml.
        """
        settings_path = Path(__file__).parent / "config" / "settings.yaml"
        with open(settings_path) as fh:
            self._cfg = yaml.safe_load(fh)

        self._dry_run = dry_run if dry_run is not None else self._cfg["bot"]["dry_run"]

        if self._dry_run:
            logger.info("[DRY-RUN] mode active - no real orders will be placed")

        # Initialise in dependency order
        logger.info("Initialising exchange wrapper …")
        self.exchange = ResilientExchangeWrapper()

        logger.info("Initialising capital manager …")
        self.capital_manager = CapitalManager(self.exchange, dry_run=self._dry_run)

        logger.info("Initialising BNB manager …")
        self.bnb_manager = BnbManager(self.exchange, self.capital_manager, dry_run=self._dry_run)

        logger.info("Initialising grid analyzer …")
        self.grid_analyzer = GridAnalyzer(self.exchange)

        logger.info("Initialising sentiment analyzer …")
        self.sentiment_analyzer = SentimentAnalyzer()

        logger.info("Initialising grid fusion …")
        self.grid_fusion = GridFusion()

        logger.info("Initialising screener …")
        self.screener = Screener(self.exchange)

        logger.info("Initialising hybrid exit manager …")
        self.exit_manager = HybridExitManager(self.exchange)

        logger.info("Initialising risk manager …")
        self.risk_manager = RiskManager(self.capital_manager)

        logger.info("Initialising Telegram bot …")
        self.telegram_app = TelegramBotApp(main_orchestrator=self)

        # Install global exception handler
        setup_global_exception_handler()

        # Note: Initial BNB check is done in run() method (async context)
        self._bnb_checked = False

        # Heartbeat / status tracking
        self._start_time: float = time.time()
        self._heartbeat_count: int = 0
        self._last_grid_ts: str = "never"
        self._last_screener_ts: str = "never"
        self._last_ema_ts: str = "never"

        logger.info("All modules initialised OK")

    # ------------------------------------------------------------------
    # Scheduled jobs
    # ------------------------------------------------------------------

    async def run_grid_analysis(self) -> None:
        """Run the 2-hour grid analysis cycle with top volume pairs.

        Steps:
            1. Get top 10 volume pairs from Binance
            2. Analyze each pair with tier-based grid levels
            3. Fetch news and sentiment for each coin
            4. Merge grids with sentiment (GridFusion → final_grid.json)
            5. Update Freqtrade config with new pairs
        """
        logger.info("=== Grid Analysis Cycle START ===")
        try:
            # Step 1: Get top 10 volume pairs
            top_pairs = await asyncio.to_thread(
                self.grid_analyzer.get_top_volume_pairs, top_n=10
            )
            logger.info(f"Top 10 volume pairs selected: {top_pairs}")

            # Step 2: Analyze each pair with tier-based grid levels
            base_grids: dict[str, any] = {}
            for rank, pair in enumerate(top_pairs):
                try:
                    grid = await asyncio.to_thread(
                        self.grid_analyzer.analyze, pair, rank=rank
                    )
                    base_grids[pair] = grid

                    # Log tier info
                    tier = (rank // 3) + 1
                    logger.info(f"{pair}: Tier {tier}, {len(grid['levels'])} levels")
                except Exception as exc:
                    logger.error(f"Failed to analyze {pair}: {exc}")

            logger.info(f"Grid analysis done: {len(base_grids)} pairs")

            # Step 3: Fetch news and sentiment (optional, with fallback)
            coins = [p.split("/")[0] for p in base_grids.keys()]
            try:
                sentiments = await self.sentiment_analyzer.get_all_sentiment_with_news_fetch(
                    coins, hours=24
                )
                logger.info(f"Sentiment analysis done: {len(sentiments)} coins")
            except Exception as exc:
                logger.warning(f"Sentiment analysis failed (using neutral): {exc}")
                sentiments = {}

            # Step 4: Fusion
            await asyncio.to_thread(self.grid_fusion.run)

            # Step 5: Update Freqtrade config with new pairs
            await self._update_freqtrade_pairs(top_pairs)

            self._last_grid_ts = datetime.now(timezone.utc).isoformat()
            logger.info("=== Grid Analysis Cycle END ===")
            send_alert_sync(f"🟢 Grid analizi tamamlandı: {len(base_grids)} coin")

        except Exception as exc:
            logger.error(f"Grid analysis cycle failed: {exc}")
            send_alert_sync(f"🔴 Grid analizi başarısız: {exc}")

    async def _update_freqtrade_pairs(self, pairs: list[str]) -> None:
        """Log selected pairs for Freqtrade (VolumePairList handles actual selection)."""
        logger.info(f"Grid analysis selected {len(pairs)} pairs for trading: {pairs}")
        # Note: VolumePairList in config.json automatically selects top volume pairs
        # This method now just logs the selection for monitoring purposes

    async def run_daily_screener(self) -> None:
        """Run the daily 00:00 UTC screener."""
        logger.info("=== Daily Screener START ===")
        try:
            candidates = await asyncio.to_thread(self.screener.daily_screener)
            self._last_screener_ts = datetime.now(timezone.utc).isoformat()

            if not candidates:
                logger.info("No screener candidates found today")
                send_alert_sync("📭 Screener: Bugün uygun aday bulunamadı")
                return

            logger.info(f"Screener found {len(candidates)} candidates")

            for candidate in candidates:
                available = self.capital_manager.check_available_balance()
                position_size = self.screener.calculate_screener_position_size(
                    candidate, available
                )

                if not self.capital_manager.can_open_screener_trade(position_size):
                    self.capital_manager.add_to_pending_queue(
                        candidate["pair"], position_size, candidate["score"]
                    )
                    continue

                if not self.risk_manager.is_trading_allowed():
                    logger.warning("Risk manager blocked screener trade")
                    break

                # Send Telegram proposal for approval
                from custom_modules.telegram_bot import send_screener_proposal  # noqa

                await send_screener_proposal(
                    candidate,
                    position_size,
                    on_buy_callback=None,   # handled by TelegramBotApp callbacks
                    on_reject_callback=None,
                )

        except Exception as exc:
            logger.error(f"Daily screener failed: {exc}")
            send_alert_sync(f"🔴 Screener başarısız: {exc}")

    async def run_ema_update(self) -> None:
        """Update EMA200 orders for all active screener exits."""
        logger.info("EMA200 update cycle")
        try:
            await asyncio.to_thread(self.exit_manager.update_ema_orders)
            self._last_ema_ts = datetime.now(timezone.utc).isoformat()
        except Exception as exc:
            logger.error(f"EMA update failed: {exc}")

    # ------------------------------------------------------------------
    # Status / Heartbeat
    # ------------------------------------------------------------------

    _STATUS_FILE = Path(__file__).parent / "logs" / "status.json"

    def _write_status(
        self,
        exchange_health: dict | None = None,
        risk_health: dict | None = None,
    ) -> None:
        """Write current bot status to logs/status.json (heartbeat).

        This file is always up-to-date so you can:
            - Copy-paste it to the AI for analysis
            - Check it in the terminal: ``type logs\\status.json``
            - Monitor it with any file-watcher

        Args:
            exchange_health: Result of exchange.health_check().
            risk_health: Result of risk_manager.health_check().
        """
        try:
            snap = self.capital_manager.get_balance_snapshot()
            now_utc = datetime.now(timezone.utc)

            status: dict = {
                "timestamp": now_utc.isoformat(),
                "uptime_seconds": round(time.time() - self._start_time, 0),
                "dry_run": self._dry_run,
                "bot_status": "running",
                "exchange": exchange_health or {},
                "risk": risk_health or {},
                "balance": {
                    "total_usdc": snap["total"],
                    "grid_locked": snap["grid_locked"],
                    "screener_locked": snap["screener_locked"],
                    "available": snap["available"],
                },
                "last_grid_analysis": self._last_grid_ts,
                "last_screener": self._last_screener_ts,
                "last_ema_update": self._last_ema_ts,
                "heartbeat_count": self._heartbeat_count,
            }

            self._STATUS_FILE.parent.mkdir(exist_ok=True)
            self._STATUS_FILE.write_text(json.dumps(status, indent=2, default=str))
            logger.debug(f"Status written: {self._STATUS_FILE}")

        except Exception as exc:
            logger.warning(f"_write_status failed: {exc}")

    async def run_health_check(self) -> None:
        """30-second health check + heartbeat."""
        try:
            exchange_health = await asyncio.to_thread(self.exchange.health_check)
            risk_health = self.risk_manager.health_check()
            self.bnb_manager.check_and_top_up()

            self._heartbeat_count += 1
            self._write_status(exchange_health, risk_health)

            if exchange_health["status"] != "healthy":
                send_alert_sync(f"⚠️ Exchange sağlık sorunu: {exchange_health}")

            if risk_health["status"] == "critical":
                logger.critical(f"Risk health critical: {risk_health}")

            # Log a visible heartbeat line every 10 cycles (~5 min)
            if self._heartbeat_count % 10 == 0:
                snap = self.capital_manager.get_balance_snapshot()
                logger.info(
                    "[HEARTBEAT] #%d | Exchange: %s | Risk: %s | "
                    "Balance: %.2f USDC | Available: %.2f USDC",
                    self._heartbeat_count,
                    exchange_health["status"],
                    risk_health["status"],
                    snap["total"],
                    snap["available"],
                )

        except Exception as exc:
            logger.error(f"Health check error: {exc}")

    async def send_daily_report_job(self) -> None:
        """Send the 00:05 UTC daily report via send_alert_sync (thread-safe)."""
        try:
            snap = self.capital_manager.get_balance_snapshot()
            risk = self.risk_manager.health_check()
            now = datetime.now(timezone.utc).strftime("%d %B %Y %H:%M UTC")
            msg = (
                f"📊 <b>GÜNLÜK ÖZET</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"🗓️ {now}\n\n"
                f"💵 <b>BAKİYE:</b>\n"
                f"├─ Total:     {snap['total']:.2f} USDC\n"
                f"├─ Grid:      {snap['grid_locked']:.2f} USDC\n"
                f"├─ Screener:  {snap['screener_locked']:.2f} USDC\n"
                f"└─ Mevcut:    {snap['available']:.2f} USDC\n\n"
                f"📈 <b>RİSK:</b>\n"
                f"├─ Durum: {risk['status']}\n"
                f"├─ Günlük P&L: {risk['daily_pnl_pct']:+.2f}%\n"
                f"└─ Ardışık Kayıp: {risk['consecutive_losses']}"
            )
            send_alert_sync(msg)
        except Exception as exc:
            logger.error(f"Daily report failed: {exc}")

    # ------------------------------------------------------------------
    # Freqtrade subprocess
    # ------------------------------------------------------------------

    async def _start_freqtrade(self) -> subprocess.Popen | None:
        """Launch Freqtrade as a background subprocess.

        Returns:
            Popen handle if started successfully, None otherwise.
        """
        project_root = Path(__file__).parent
        ft_dir = project_root / "freqtrade"
        config_path = ft_dir / "user_data" / "config.json"

        if not config_path.exists():
            logger.warning("Freqtrade config not found at %s — skipping Freqtrade launch", config_path)
            return None

        cmd = [
            sys.executable, "-m", "freqtrade", "trade",
            "--config", str(config_path),
            "--strategy", "DynamicGridStrategy",
            "--logfile", str(ft_dir / "user_data" / "logs" / "freqtrade.log"),
        ]
        if self._dry_run:
            cmd.append("--dry-run")

        log_file = project_root / "logs" / "freqtrade_subprocess.log"
        log_file.parent.mkdir(exist_ok=True)

        try:
            ft_log = open(log_file, "a", encoding="utf-8")  # noqa: SIM115
            process = subprocess.Popen(
                cmd,
                cwd=str(ft_dir),
                stdout=ft_log,
                stderr=ft_log,
                env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8"},
            )
            logger.info("Freqtrade subprocess started (PID=%d) — log: %s", process.pid, log_file)
            send_alert_sync(f"Freqtrade baslatildi (PID={process.pid})")
            return process
        except Exception as exc:
            logger.error("Failed to start Freqtrade subprocess: %s", exc)
            send_alert_sync(f"Freqtrade baslatma hatasi: {exc}")
            return None

    async def _send_startup_grid_notification(self) -> None:
        """Send grid levels for each coin to Telegram on startup."""
        try:
            final_grid_file = Path(__file__).parent / "data" / "final_grid.json"
            if not final_grid_file.exists():
                logger.warning("final_grid.json not found, skipping startup notification")
                return

            with open(final_grid_file, encoding="utf-8") as f:
                grids = json.load(f)

            coin_list = list(grids.keys())
            if not coin_list:
                logger.warning("No coins in final_grid.json")
                return

            # Summary message
            summary = (
                f"🚀 <b>Bot Başlatıldı - Grid Analizi Tamamlandı</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"📊 <b>{len(coin_list)} Coin Bulundu:</b>\n"
            )
            for i, pair in enumerate(coin_list, 1):
                grid = grids[pair]
                levels_count = len(grid.get("levels", []))
                tier = grid.get("spacing", "unknown")
                summary += f"  {i}. {pair} - {levels_count} seviye ({tier})\n"

            send_alert_sync(summary)
            await asyncio.sleep(0.5)  # Rate limit protection

            # Detailed message for each coin
            for pair, grid in grids.items():
                levels = grid.get("levels", [])
                if not levels:
                    continue

                # Show first 5 levels
                levels_str = "\n".join([f"    ${l:,.2f}" for l in levels[:5]])
                if len(levels) > 5:
                    levels_str += f"\n    ... ve {len(levels)-5} seviye daha"

                detail = (
                    f"📈 <b>{pair}</b>\n"
                    f"  Seviyeler ({len(levels)}):\n{levels_str}\n"
                    f"  Upper: ${grid.get('upper_bound', 0):,.2f}\n"
                    f"  Lower: ${grid.get('lower_bound', 0):,.2f}\n"
                    f"  Pozisyon: ${grid.get('position_size', 0):,.2f} USDC\n"
                )
                send_alert_sync(detail)
                await asyncio.sleep(0.3)  # Rate limit protection

            logger.info(f"Startup grid notification sent for {len(coin_list)} coins")

        except Exception as exc:
            logger.error(f"Startup grid notification failed: {exc}")

    # ------------------------------------------------------------------
    # Main event loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Start all background tasks and the Telegram bot.

        Schedule:
            - Grid analysis:   every 2 hours
            - Screener:        once at 00:00 UTC (simulated via 24 h interval)
            - EMA update:      every 4 hours
            - Health check:    every 30 seconds
            - Daily report:    once at 00:05 UTC
            - Telegram polling: continuous
        """
        logger.info("Bot starting ...")
        send_alert_sync("🤖 Akıllı Grid Trading Bot başlatıldı!")

        # Initial BNB check on startup (in async context)
        if not getattr(self, '_bnb_checked', False):
            logger.info("Performing initial BNB balance check ...")
            try:
                bnb_status = await asyncio.to_thread(self.bnb_manager.check_and_top_up)
                if bnb_status["triggered"]:
                    logger.info(f"BNB auto-buy triggered on startup: {bnb_status}")
                else:
                    logger.info(f"BNB balance sufficient: {bnb_status.get('bnb_balance_usdc', 0):.4f} USDC")
                self._bnb_checked = True
            except Exception as exc:
                logger.warning(f"Initial BNB check failed (non-critical): {exc}")

        # Launch Freqtrade as a subprocess
        ft_process = await self._start_freqtrade()

        # Launch Telegram bot in background
        tg_task = asyncio.create_task(self.telegram_app.start_polling())

        # WebSocket monitor
        ws_task = asyncio.create_task(self.exchange.monitor_websocket())

        # === IMMEDIATE STARTUP GRID ANALYSIS ===
        logger.info("=== Running immediate grid analysis on startup ===")
        try:
            await self.run_grid_analysis()
            await self._send_startup_grid_notification()
        except Exception as exc:
            logger.error(f"Startup grid analysis failed: {exc}")
            send_alert_sync(f"⚠️ Başlangıç grid analizi başarısız: {exc}")

        # Scheduler loop
        last_grid = 0.0
        last_screener = -999999.0  # Force immediate screener on startup
        last_ema = 0.0
        last_health = 0.0
        last_report = 0.0

        GRID_INTERVAL = 2 * 3600        # 2 hours
        SCREENER_INTERVAL = 1 * 3600    # 1 hour (changed from 24h)
        EMA_INTERVAL = 1 * 3600         # 1 hour (changed from 4h)
        HEALTH_INTERVAL = 30            # 30 seconds
        REPORT_INTERVAL = 24 * 3600     # 24 hours

        try:
            while True:
                now = time.time()

                if now - last_health >= HEALTH_INTERVAL:
                    await self.run_health_check()
                    last_health = now

                    # Check if Freqtrade subprocess is still alive
                    if ft_process and ft_process.poll() is not None:
                        logger.warning(
                            "Freqtrade subprocess exited (code=%d) — restarting ...",
                            ft_process.returncode,
                        )
                        send_alert_sync(
                            f"Freqtrade durdu (kod={ft_process.returncode}), yeniden baslatiliyor..."
                        )
                        ft_process = await self._start_freqtrade()

                if now - last_grid >= GRID_INTERVAL:
                    await self.run_grid_analysis()
                    last_grid = now

                if now - last_ema >= EMA_INTERVAL:
                    await self.run_ema_update()
                    last_ema = now

                if now - last_screener >= SCREENER_INTERVAL:
                    await self.run_daily_screener()
                    last_screener = now

                if now - last_report >= REPORT_INTERVAL:
                    await self.send_daily_report_job()
                    last_report = now

                await asyncio.sleep(10)  # poll interval

        except asyncio.CancelledError:
            logger.info("Orchestrator loop cancelled")
        finally:
            tg_task.cancel()
            ws_task.cancel()
            if ft_process and ft_process.returncode is None:
                logger.info("Stopping Freqtrade subprocess ...")
                ft_process.terminate()
                try:
                    await asyncio.wait_for(ft_process.wait(), timeout=10)
                except asyncio.TimeoutError:
                    ft_process.kill()
            logger.info("Bot stopped")
            send_alert_sync("⛔ Bot durduruldu")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """Parse CLI arguments and start the bot."""
    parser = argparse.ArgumentParser(description="Akıllı Grid Trading Bot")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Override settings.yaml dry_run=true",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level",
    )
    args = parser.parse_args()

    # Re-setup logging with CLI-specified level
    setup_logging(args.log_level)

    orchestrator = BotOrchestrator(dry_run=args.dry_run if args.dry_run else None)

    try:
        asyncio.run(orchestrator.run())
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
    except Exception as exc:
        logger.critical(f"Fatal error: {exc}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
