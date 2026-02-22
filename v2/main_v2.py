"""main_v2.py — Grid Bot v2 Orkestratörü.

Freqtrade subprocess'i kaldırır.
GridEngine ile emirleri doğrudan Binance'e gönderir.
Mevcut tüm modülleri (grid_analyzer, sentiment, screener, vb.) olduğu gibi kullanır.

Kullanım:
    cd my_ft/v2
    python main_v2.py              # Dry-run (varsayılan, güvenli)
    python main_v2.py --live       # Canlı mod (dikkatli!)
"""

import argparse
import asyncio
import json
import logging
import logging.handlers
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

# ---------------------------------------------------------------------------
# Loglama kurulumu (diğer importlardan önce)
# ---------------------------------------------------------------------------

def setup_logging(log_level: str = "INFO") -> None:
    log_dir = Path(__file__).parent.parent / "logs"
    log_dir.mkdir(exist_ok=True)

    fmt = "%(asctime)s [%(levelname)-8s] %(name)s: %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"
    formatter = logging.Formatter(fmt, datefmt)

    handlers: list[logging.Handler] = [
        logging.StreamHandler(sys.stdout),
        logging.handlers.RotatingFileHandler(
            log_dir / "v2_bot.log", maxBytes=52_428_800, backupCount=7
        ),
    ]
    for h in handlers:
        h.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(getattr(logging, log_level.upper(), logging.INFO))
    for existing in list(root.handlers):
        root.removeHandler(existing)
    for h in handlers:
        root.addHandler(h)


setup_logging()
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Modül importları — ana repo kök dizinini path'e ekle
# ---------------------------------------------------------------------------

sys.path.insert(0, str(Path(__file__).parent.parent))

from custom_modules.api_wrapper import ResilientExchangeWrapper, setup_global_exception_handler
from custom_modules.capital_manager import CapitalManager
from custom_modules.risk_manager import RiskManager
from custom_modules.bnb_manager import BnbManager
from custom_modules.grid_analyzer import GridAnalyzer
from custom_modules.sentiment_analyzer import SentimentAnalyzer
from custom_modules.grid_fusion import GridFusion
from custom_modules.screener import Screener
from custom_modules.hybrid_exit import HybridExitManager
from custom_modules.news_fetcher import NewsFetcher
from custom_modules.telegram_bot import (
    TelegramBotApp,
    send_alert_sync,
    send_daily_report,
)
from grid_engine import GridEngine


# ---------------------------------------------------------------------------
# Yardımcı: final_grid.json okuma
# ---------------------------------------------------------------------------

def _load_levels_from_json(path: Path) -> dict:
    """
    Grid seviyelerini JSON dosyasından oku.
    GridFusion çıktısı (final_grid.json) veya GridAnalyzer çıktısı (base_grid.json) olabilir.

    Dönen format: {"BTC/USDC": [94000, 95000, ...], ...}
    """
    if not path.exists():
        logger.warning("Seviye dosyası bulunamadı: %s", path)
        return {}
    try:
        raw = json.loads(path.read_text())
        result = {}
        for pair, data in raw.items():
            if isinstance(data, dict):
                levels = data.get("levels", [])
            elif isinstance(data, list):
                levels = data
            else:
                continue
            if levels:
                result[pair] = [float(l) for l in levels]
        return result
    except Exception as e:
        logger.error("Seviye dosyası okunamadı %s: %s", path, e)
        return {}


# ---------------------------------------------------------------------------
# Ana orkestratör
# ---------------------------------------------------------------------------

class BotOrchestratorV2:
    """
    v2 Bot Orkestratörü.

    Freqtrade subprocess yoktur.
    Tüm grid emirleri GridEngine üzerinden Binance'e gönderilir.
    Screener, sentiment, risk ve diğer modüller değişmeden kullanılır.
    """

    FINAL_GRID = Path(__file__).parent.parent / "data" / "final_grid.json"
    BASE_GRID  = Path(__file__).parent.parent / "data" / "base_grid.json"
    STATUS_FILE = Path(__file__).parent.parent / "logs" / "v2_status.json"

    def __init__(self, dry_run: bool = True) -> None:
        cfg_path = Path(__file__).parent.parent / "config" / "settings.yaml"
        with open(cfg_path) as f:
            self.settings = yaml.safe_load(f)

        if dry_run:
            self.settings["bot"]["dry_run"] = True
        self.dry_run = self.settings["bot"]["dry_run"]

        self._setup_modules()
        self._heartbeat = 0

    def _setup_modules(self) -> None:
        """Tüm modülleri başlat."""
        self.exchange = ResilientExchangeWrapper()
        self.capital  = CapitalManager(exchange_wrapper=self.exchange, dry_run=self.dry_run)
        self.risk     = RiskManager(capital_manager=self.capital)
        self.bnb      = BnbManager(
            exchange_wrapper=self.exchange,
            capital_manager=self.capital,
            dry_run=self.dry_run,
        )
        self.analyzer = GridAnalyzer(exchange_wrapper=self.exchange)
        self.sentiment = SentimentAnalyzer()
        self.news     = NewsFetcher()
        self.fusion   = GridFusion()
        self.screener = Screener(exchange_wrapper=self.exchange)
        self.exit_mgr = HybridExitManager(exchange_wrapper=self.exchange)

        self.grid_engine = GridEngine(
            exchange=self.exchange,
            capital=self.capital,
            risk=self.risk,
            dry_run=self.dry_run,
        )

        # Telegram bot uygulaması (komut handler'larıyla)
        self.tg_app = TelegramBotApp(main_orchestrator=self)

        # Coin konfigürasyonu
        coins_path = Path(__file__).parent.parent / "config" / "coins.yaml"
        with open(coins_path) as f:
            coins_cfg = yaml.safe_load(f)
        self.grid_coins: list[str] = coins_cfg.get("grid_coins", [])

        logger.info("v2 modülleri hazır: %s", self.grid_coins)

    # ------------------------------------------------------------------
    # Ana çalışma döngüsü
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Tüm zamanlanmış görevleri başlat, ardından GridEngine'i çalıştır."""
        setup_global_exception_handler()

        logger.info(
            "Grid Bot v2 başlatılıyor [%s]",
            "DRY-RUN" if self.dry_run else "CANLI"
        )

        # 1. İlk grid analizi — seviyeleri hesapla
        await self._run_analysis_cycle()

        # 2. Arka plan görevlerini başlat
        tasks = [
            asyncio.create_task(self._schedule_grid_analysis(),    name="grid_analysis"),
            asyncio.create_task(self._schedule_screener(),         name="screener"),
            asyncio.create_task(self._schedule_health_check(),     name="health_check"),
            asyncio.create_task(self._schedule_bnb_check(),        name="bnb_check"),
            asyncio.create_task(self._schedule_ema_update(),       name="ema_update"),
            asyncio.create_task(self._schedule_daily_report(),     name="daily_report"),
            asyncio.create_task(self._schedule_grid_notification(), name="grid_notify"),
            asyncio.create_task(self.tg_app.start_polling(),       name="telegram"),
        ]

        # 3. GridEngine fill izleyiciyi başlat (en kritik döngü)
        levels = self._best_available_levels()
        try:
            engine_task = asyncio.create_task(
                self.grid_engine.start(levels), name="grid_engine"
            )
            await asyncio.gather(engine_task, *tasks)
        except asyncio.CancelledError:
            logger.info("Bot durduruldu, görevler temizleniyor")
        finally:
            self.grid_engine.stop()
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            logger.info("Grid Bot v2 tamamen durduruldu")

    def _best_available_levels(self) -> dict:
        """final_grid.json varsa onu, yoksa base_grid.json'ı kullan."""
        levels = _load_levels_from_json(self.FINAL_GRID)
        if levels:
            logger.info("Seviyeler final_grid.json'dan yüklendi")
            return levels
        levels = _load_levels_from_json(self.BASE_GRID)
        if levels:
            logger.warning("final_grid.json yok, base_grid.json kullanılıyor")
            return levels
        logger.error("Hiç seviye dosyası bulunamadı! Önce grid analizi yapılmalı.")
        return {}

    # ------------------------------------------------------------------
    # Grid analizi döngüsü (her 2 saatte bir)
    # ------------------------------------------------------------------

    async def _run_analysis_cycle(self) -> None:
        """Teknik analiz → Sentiment → Fusion → GridEngine güncelle."""
        logger.info("Grid analiz döngüsü başlıyor")

        # Teknik analiz
        try:
            await asyncio.to_thread(self.analyzer.analyze_all)
        except Exception as e:
            logger.error("Grid analiz hatası: %s", e)

        # Sentiment analizi (SentimentAnalyzer kendi haberlerini çeker)
        try:
            coins = [c.split("/")[0] for c in self.grid_coins]
            await self.sentiment.get_all_sentiment_with_news_fetch(coins, hours=24)
            logger.info("Sentiment analizi tamamlandı")
        except Exception as e:
            logger.warning("Sentiment analizi başarısız (bot devam ediyor): %s", e)

        # Fusion: teknik + sentiment birleştir
        try:
            await asyncio.to_thread(self.fusion.run)
        except Exception as e:
            logger.error("Fusion hatası: %s", e)

        # GridEngine'i yeni seviyelerle güncelle
        new_levels = self._best_available_levels()
        if new_levels:
            for pair, levels in new_levels.items():
                if pair in self.grid_coins and levels:
                    await self.grid_engine.adapt_grid(pair, levels)

    async def _schedule_grid_analysis(self) -> None:
        """Her 2 saatte bir grid analizi çalıştır."""
        interval = self.settings["grid"]["analysis_interval_hours"] * 3600
        while True:
            await asyncio.sleep(interval)
            try:
                await self._run_analysis_cycle()
            except Exception as e:
                logger.error("Zamanlanmış grid analizi hatası: %s", e)

    # ------------------------------------------------------------------
    # Screener (her gün 00:00 UTC)
    # ------------------------------------------------------------------

    async def _schedule_screener(self) -> None:
        """Gece yarısı UTC'de günlük screener çalıştır."""
        while True:
            now = datetime.now(timezone.utc)
            seconds_to_midnight = (
                (23 - now.hour) * 3600
                + (59 - now.minute) * 60
                + (60 - now.second)
            )
            await asyncio.sleep(max(seconds_to_midnight, 60))
            try:
                await asyncio.to_thread(self.screener.daily_screener)
            except Exception as e:
                logger.error("Screener hatası: %s", e)
            await asyncio.sleep(3600)  # Çifte tetiklenmeyi önle

    # ------------------------------------------------------------------
    # Health check (her 30 saniyede)
    # ------------------------------------------------------------------

    async def _schedule_health_check(self) -> None:
        """Binance bağlantısını ve risk durumunu kontrol et."""
        while True:
            await asyncio.sleep(30)
            try:
                self._heartbeat += 1
                health = await asyncio.to_thread(self.exchange.health_check)
                risk   = self.risk.health_check()

                status = {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "heartbeat": self._heartbeat,
                    "dry_run": self.dry_run,
                    "bot_status": "running",
                    "exchange": health,
                    "risk": risk,
                    "grid_engine": self.grid_engine.get_stats(),
                }
                self.STATUS_FILE.parent.mkdir(exist_ok=True)
                self.STATUS_FILE.write_text(json.dumps(status, indent=2))

                if health.get("status") != "healthy":
                    logger.warning("Exchange sağlık sorunu: %s", health)
                if risk.get("status") == "critical":
                    logger.warning("Risk durumu kritik: %s", risk)

            except Exception as e:
                logger.error("Health check hatası: %s", e)

    # ------------------------------------------------------------------
    # BNB kontrolü (her 15 dakika)
    # ------------------------------------------------------------------

    async def _schedule_bnb_check(self) -> None:
        """BNB bakiyesi düştüğünde otomatik alım yap."""
        interval = self.settings["bnb"]["check_interval_minutes"] * 60
        while True:
            await asyncio.sleep(interval)
            try:
                await asyncio.to_thread(self.bnb.check_and_top_up)
            except Exception as e:
                logger.error("BNB check hatası: %s", e)

    # ------------------------------------------------------------------
    # EMA güncelleme (her 4 saatte)
    # ------------------------------------------------------------------

    async def _schedule_ema_update(self) -> None:
        """Screener pozisyonları için EMA200 çıkış emirlerini güncelle."""
        interval = self.settings["grid"]["ema_update_interval_hours"] * 3600
        while True:
            await asyncio.sleep(interval)
            try:
                await asyncio.to_thread(self.exit_mgr.update_ema_orders)
            except Exception as e:
                logger.error("EMA güncelleme hatası: %s", e)

    # ------------------------------------------------------------------
    # Günlük rapor (her gün 00:05 UTC)
    # ------------------------------------------------------------------

    async def _schedule_daily_report(self) -> None:
        """00:05 UTC'de günlük P&L raporu gönder."""
        while True:
            now = datetime.now(timezone.utc)
            # Bir sonraki 00:05 UTC'ye kadar bekle
            seconds_to_report = (
                (23 - now.hour) * 3600
                + (59 - now.minute) * 60
                + (60 - now.second)
                + 5 * 60
            )
            await asyncio.sleep(seconds_to_report % 86400 or 86400)
            try:
                snap  = self.capital.get_balance_snapshot()
                stats = self.grid_engine.get_stats()
                await send_daily_report({
                    "daily_pnl":      stats.get("total_pnl_usdc", 0),
                    "daily_pnl_pct":  stats.get("total_pnl_usdc", 0) / self.capital._total_usdc * 100,
                    "weekly_pnl":     0,
                    "monthly_pnl":    0,
                    "total_pnl":      stats.get("total_pnl_usdc", 0),
                    "total_trades":   stats.get("total_cycles", 0),
                    "winning_trades": stats.get("total_cycles", 0),
                    "losing_trades":  0,
                    "win_rate":       100.0,
                    "total_balance":  snap["total"],
                    "grid_locked":    snap["grid_locked"],
                    "screener_locked": snap["screener_locked"],
                    "available":      snap["available"],
                })
            except Exception as e:
                logger.error("Günlük rapor hatası: %s", e)

    # ------------------------------------------------------------------
    # Grid bildirim (her 5 dakika)
    # ------------------------------------------------------------------

    async def _schedule_grid_notification(self) -> None:
        """Her 5 dakikada grid özetini logla."""
        interval = self.settings["grid"].get("notify_interval_minutes", 5) * 60
        while True:
            await asyncio.sleep(interval)
            try:
                status = self.grid_engine.get_status_text()
                logger.info("Grid özeti:\n%s", status)
            except Exception as e:
                logger.debug("Grid bildirim hatası: %s", e)


# ---------------------------------------------------------------------------
# Giriş noktası
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Grid Bot v2 — Freqtrade'siz")
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--live", action="store_true",
        help="Canlı mod (DRY-RUN'ı devre dışı bırakır — DİKKATLİ!)"
    )
    group.add_argument(
        "--dry-run", action="store_true", default=True,
        help="Kağıt ticaret modu (varsayılan, güvenli)"
    )
    args = parser.parse_args()
    dry_run = not args.live

    if not dry_run:
        print("⚠️  CANLI MOD AKTİF — gerçek para işlemleri yapılacak!")
        print("   5 saniye içinde Ctrl+C ile iptal edebilirsiniz …")
        import time; time.sleep(5)

    bot = BotOrchestratorV2(dry_run=dry_run)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def _shutdown(sig, _frame):
        logger.info("Kapatma sinyali alındı (%s)", sig.name)
        bot.grid_engine.stop()
        for task in asyncio.all_tasks(loop):
            task.cancel()

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, _shutdown)

    try:
        loop.run_until_complete(bot.run())
    except KeyboardInterrupt:
        logger.info("Kullanıcı tarafından durduruldu")
    finally:
        loop.close()
        logger.info("Bot tamamen kapatıldı")


if __name__ == "__main__":
    main()
