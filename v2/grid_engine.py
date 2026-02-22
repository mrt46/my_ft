"""GridEngine v2 — Doğrudan Binance Emir Yönetimi.

Freqtrade'in yerini alır. Tüm grid emirlerini direkt Binance'e gönderir.

Akış:
  1. final_grid.json'dan (GridFusion çıktısı) seviyeleri al
  2. Her koin için mevcut fiyat altındaki seviyelere limit BUY emri koy
  3. Her 5 saniyede fill kontrolü yap
  4. BUY dolunca → anında üst seviyeye SELL emri koy
  5. SELL dolunca → kâr kaydet → aynı seviyeye yeni BUY koy (döngü)
  6. Her 2 saatte bir → seviyeleri yeniden hesapla, eski emirleri iptal, yenilerini koy

Mevcut modülleri değiştirmez:
  - grid_analyzer.py  → Seviye hesaplama
  - grid_fusion.py    → Sentiment birleştirme
  - capital_manager.py → Sermaye takibi
  - risk_manager.py   → Circuit breaker
  - telegram_bot.py   → Bildirimler
  - api_wrapper.py    → Binance iletişimi
"""

import asyncio
import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Veri sınıfları
# ---------------------------------------------------------------------------

@dataclass
class GridOrder:
    """Bir grid seviyesinin emir çiftini temsil eder (alış + satış)."""

    pair: str
    level_price: float          # Bu emrin hedeflediği S/R seviyesi
    buy_order_id: str
    buy_price: float
    buy_qty: float
    usdc_allocated: float
    state: str = "PENDING_BUY"  # PENDING_BUY | SELL_PENDING | COMPLETED | CANCELLED
    sell_order_id: Optional[str] = None
    sell_price: Optional[float] = None
    buy_fill_time: Optional[str] = None
    sell_fill_time: Optional[str] = None
    realized_pnl_usdc: float = 0.0


@dataclass
class GridState:
    """Bir koin için tüm grid durumunu tutar."""

    pair: str
    levels: List[float]
    active_orders: Dict[str, GridOrder] = field(default_factory=dict)
    total_realized_pnl: float = 0.0
    grid_cycles: int = 0
    last_adapted: Optional[str] = None
    current_price: float = 0.0


# ---------------------------------------------------------------------------
# GridEngine
# ---------------------------------------------------------------------------

class GridEngine:
    """
    v2 Grid Motor — Mevcut modüllerle entegre, Freqtrade'siz.

    Parametre olarak mevcut modül örneklerini alır:
        exchange  : ResilientExchangeWrapper (senkron ccxt sarıcı)
        capital   : CapitalManager
        risk      : RiskManager
        dry_run   : bool
    """

    DATA_FILE = Path(__file__).parent.parent / "data" / "v2" / "grid_orders.json"
    POLL_INTERVAL_SEC = 5       # Fill kontrolü sıklığı
    GRID_SPACING_BUFFER = 0.001 # Satış fiyatına eklenen %0.1 tampon

    # Rank → (koin başına USDC, seviye sayısı)
    TIER_CONFIG = {
        0: (120, 10),  # BTC — Tier 1
        1: (120, 10),  # ETH — Tier 1
        2: (105,  8),  # BNB — Tier 2
        3: (105,  8),  # SOL — Tier 2
        4: (150,  6),  # XRP — Tier 3
    }
    RANK_MAP = {
        "BTC/USDC": 0,
        "ETH/USDC": 1,
        "BNB/USDC": 2,
        "SOL/USDC": 3,
        "XRP/USDC": 4,
    }

    def __init__(
        self,
        exchange,
        capital,
        risk,
        dry_run: bool = True,
    ) -> None:
        self.exchange = exchange
        self.capital = capital
        self.risk = risk
        self.dry_run = dry_run
        self._running = False
        self._grids: Dict[str, GridState] = {}
        self._load_state()

    # ------------------------------------------------------------------
    # Ana giriş noktası
    # ------------------------------------------------------------------

    async def start(self, coins_with_levels: Dict[str, List[float]]) -> None:
        """
        Tüm koinler için grid başlat, ardından fill döngüsüne gir.

        Args:
            coins_with_levels: {pair: [seviye1, seviye2, ...]}
        """
        self._running = True
        logger.info("GridEngine başlatılıyor: %s", list(coins_with_levels.keys()))

        from custom_modules.telegram_bot import send_alert_sync
        send_alert_sync(
            f"🔲 <b>Grid Engine v2 başlatıldı</b>\n"
            f"{'🧪 DRY-RUN' if self.dry_run else '🟢 CANLI'}\n"
            f"📅 {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC\n"
            f"📊 Koin: {', '.join(coins_with_levels.keys())}"
        )

        for pair, levels in coins_with_levels.items():
            if levels:
                await self._init_coin_grid(pair, levels)

        self._save_state()
        logger.info("GridEngine: tüm grid emirleri kuruldu, fill izleyici başlıyor")
        await self._fill_monitor_loop()

    async def adapt_grid(self, pair: str, new_levels: List[float]) -> None:
        """
        2 saatlik grid analizi sonrası seviyeleri güncelle.
        Eski emirleri iptal et, yeni seviyelere yenilerini koy.
        """
        gs = self._grids.get(pair)
        if not gs or not new_levels:
            return

        old_set = set(round(l, 8) for l in gs.levels)
        new_set = set(round(l, 8) for l in new_levels)
        stale_levels = old_set - new_set
        fresh_levels = new_set - old_set

        cancelled = 0
        for order_id, go in list(gs.active_orders.items()):
            if go.state == "PENDING_BUY" and round(go.level_price, 8) in stale_levels:
                success = await asyncio.to_thread(
                    self.exchange.cancel_order, order_id, pair
                )
                if success:
                    go.state = "CANCELLED"
                    cancelled += 1

        ticker = await asyncio.to_thread(self.exchange.fetch_ticker, pair)
        current_price = ticker["last"]
        gs.current_price = current_price

        rank = self.RANK_MAP.get(pair, 4)
        per_level_usdc = self.capital.get_tier_allocation(rank)["per_level_usdc"]

        placed = 0
        for level in sorted(fresh_levels, reverse=True):
            if level < current_price * 0.999:
                qty = per_level_usdc / level
                if qty * level >= 5.0:
                    order_id = await self._place_buy(pair, qty, level, gs)
                    if order_id:
                        placed += 1

        gs.levels = new_levels
        gs.last_adapted = datetime.now(timezone.utc).isoformat()
        self._save_state()

        logger.info("GridEngine adapt: %s → iptal=%d yeni=%d", pair, cancelled, placed)

        if cancelled > 0 or placed > 0:
            from custom_modules.telegram_bot import send_alert_sync
            send_alert_sync(
                f"🔄 <b>Grid güncellendi</b>: {pair}\n"
                f"❌ İptal edilen: {cancelled} eski emir\n"
                f"✅ Yeni: {placed} emir\n"
                f"📊 Toplam seviye: {len(new_levels)}"
            )

    def stop(self) -> None:
        """Fill izleyiciyi durdur ve durumu kaydet."""
        self._running = False
        self._save_state()
        logger.info("GridEngine durduruldu")

    # ------------------------------------------------------------------
    # Başlangıç: bir koin için grid kurulumu
    # ------------------------------------------------------------------

    async def _init_coin_grid(self, pair: str, levels: List[float]) -> None:
        """Mevcut fiyatın altındaki tüm seviyelere BUY emri koy."""
        try:
            ticker = await asyncio.to_thread(self.exchange.fetch_ticker, pair)
            current_price = ticker["last"]
        except Exception as e:
            logger.error("GridEngine: %s fiyat alınamadı: %s", pair, e)
            return

        buy_levels = [l for l in sorted(levels) if l < current_price * 0.999]
        if not buy_levels:
            logger.warning("GridEngine: %s için mevcut fiyat altında seviye yok (fiyat=%.4f)", pair, current_price)
            return

        rank = self.RANK_MAP.get(pair, 4)
        per_level_usdc = self.capital.get_tier_allocation(rank)["per_level_usdc"]

        # Daha önce bu koin için state varsa koru, yoksa yeni oluştur
        if pair not in self._grids:
            self._grids[pair] = GridState(pair=pair, levels=levels, current_price=current_price)
        else:
            self._grids[pair].levels = levels
            self._grids[pair].current_price = current_price

        gs = self._grids[pair]
        placed = 0

        for level in buy_levels:
            qty = per_level_usdc / level
            if qty * level < 5.0:   # Binance minimum notional ~5 USDC
                logger.debug("GridEngine: %s @ %.4f çok küçük, atlanıyor", pair, level)
                continue

            # Bu seviyede zaten aktif emir var mı?
            already_exists = any(
                abs(go.level_price - level) / level < 0.0005 and go.state in ("PENDING_BUY", "SELL_PENDING")
                for go in gs.active_orders.values()
            )
            if already_exists:
                continue

            order_id = await self._place_buy(pair, qty, level, gs)
            if order_id:
                placed += 1

        gs.last_adapted = datetime.now(timezone.utc).isoformat()
        logger.info("GridEngine: %s başlatıldı — %d/%d alış emri @ fiyat=%.4f", pair, placed, len(buy_levels), current_price)

        from custom_modules.telegram_bot import send_alert_sync
        send_alert_sync(
            f"🔲 <b>Grid kuruldu</b>: {pair}\n"
            f"💰 Mevcut fiyat: {current_price:.4f}\n"
            f"📊 Aktif alış emri: {placed}\n"
            f"💵 Seviye başı: {per_level_usdc:.2f} USDC\n"
            f"{'🧪 DRY-RUN' if self.dry_run else '🟢 CANLI'}"
        )

    # ------------------------------------------------------------------
    # Fill izleyici (ana döngü)
    # ------------------------------------------------------------------

    async def _fill_monitor_loop(self) -> None:
        """Her 5 saniyede açık emirleri kontrol et, fill olanları işle."""
        logger.info("GridEngine: fill izleyici başladı (polling=%ds)", self.POLL_INTERVAL_SEC)

        while self._running:
            try:
                for pair in list(self._grids.keys()):
                    if self.risk.is_trading_allowed():
                        await self._check_fills(pair)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("GridEngine fill izleyici hatası: %s", e)

            await asyncio.sleep(self.POLL_INTERVAL_SEC)

    async def _check_fills(self, pair: str) -> None:
        """Bir koin için bekleyen emirlerin fill durumunu kontrol et."""
        gs = self._grids.get(pair)
        if not gs:
            return

        pending = {
            oid: go for oid, go in gs.active_orders.items()
            if go.state in ("PENDING_BUY", "SELL_PENDING")
        }
        if not pending:
            return

        if self.dry_run:
            await self._simulate_fills(pair, gs, pending)
            return

        changed = False
        for order_id, go in list(pending.items()):
            try:
                raw = await asyncio.to_thread(
                    self.exchange.exchange.fetch_order, order_id, pair
                )
                if raw["status"] == "closed":
                    fill_price = float(raw.get("average") or raw.get("price") or go.buy_price)
                    fill_qty = float(raw["filled"])
                    if go.state == "PENDING_BUY":
                        await self._on_buy_filled(pair, gs, go, fill_price, fill_qty)
                    elif go.state == "SELL_PENDING":
                        await self._on_sell_filled(pair, gs, go, fill_price, fill_qty)
                    changed = True
            except Exception as e:
                logger.debug("GridEngine: emir kontrol %s: %s", order_id, e)

        if changed:
            self._save_state()

    async def _simulate_fills(self, pair: str, gs: GridState, pending: dict) -> None:
        """Dry-run: fiyat seviyeyi geçince fill simülasyonu yap."""
        try:
            ticker = await asyncio.to_thread(self.exchange.fetch_ticker, pair)
            price = ticker["last"]
            gs.current_price = price
        except Exception:
            return

        changed = False
        for order_id, go in list(pending.items()):
            if go.state == "PENDING_BUY" and price <= go.buy_price:
                await self._on_buy_filled(pair, gs, go, go.buy_price, go.buy_qty)
                changed = True
            elif go.state == "SELL_PENDING" and go.sell_price and price >= go.sell_price:
                await self._on_sell_filled(pair, gs, go, go.sell_price, go.buy_qty)
                changed = True

        if changed:
            self._save_state()

    # ------------------------------------------------------------------
    # Fill olayları
    # ------------------------------------------------------------------

    async def _on_buy_filled(
        self, pair: str, gs: GridState, go: GridOrder, fill_price: float, fill_qty: float
    ) -> None:
        """Alış doldu → anında satış emri koy."""
        go.state = "BUY_FILLED"
        go.buy_fill_time = datetime.now(timezone.utc).isoformat()

        sell_price = self._calc_sell_price(fill_price, gs.levels)
        go.sell_price = sell_price

        logger.info(
            "GridEngine ✅ ALIŞ DOLDU: %s @ %.4f qty=%.6f → satış @ %.4f",
            pair, fill_price, fill_qty, sell_price
        )

        # Sermaye kilitle
        self.capital.lock_grid(
            pair=pair,
            amount_usdc=fill_qty * fill_price,
            entry_price=fill_price,
            amount_coin=fill_qty,
        )

        # Satış emri koy
        sell_id = await self._place_sell(pair, fill_qty, sell_price, go, gs)
        if sell_id:
            go.sell_order_id = sell_id
            go.state = "SELL_PENDING"

        expected_pnl = (sell_price - fill_price) * fill_qty

        from custom_modules.telegram_bot import send_alert_sync
        send_alert_sync(
            f"✅ <b>Alış doldu</b>: {pair}\n"
            f"💵 Fiyat: {fill_price:.4f} USDC\n"
            f"📦 Miktar: {fill_qty:.6f}\n"
            f"🎯 Satış hedefi: {sell_price:.4f} USDC\n"
            f"💰 Beklenen kâr: +{expected_pnl:.2f} USDC"
        )

    async def _on_sell_filled(
        self, pair: str, gs: GridState, go: GridOrder, fill_price: float, fill_qty: float
    ) -> None:
        """Satış doldu → kâr kaydet, aynı seviyeye yeni alış koy."""
        go.state = "COMPLETED"
        go.sell_fill_time = datetime.now(timezone.utc).isoformat()

        pnl = (fill_price - go.buy_price) * fill_qty
        go.realized_pnl_usdc = pnl
        gs.total_realized_pnl += pnl
        gs.grid_cycles += 1

        logger.info(
            "GridEngine 💰 SATIŞ DOLDU: %s @ %.4f PNL=%+.2f USDC döngü=#%d",
            pair, fill_price, pnl, gs.grid_cycles
        )

        # Risk yöneticisine bildir
        pnl_pct = (fill_price - go.buy_price) / go.buy_price
        self.risk.record_trade_result(pnl, pnl_pct)

        # Sermaye serbest bırak
        self.capital.release(pair, "grid")

        emoji = "💰" if pnl > 0 else "📉"
        from custom_modules.telegram_bot import send_alert_sync
        send_alert_sync(
            f"{emoji} <b>Satış doldu</b>: {pair}\n"
            f"💵 Satış: {fill_price:.4f} | Alış: {go.buy_price:.4f}\n"
            f"📦 Miktar: {fill_qty:.6f}\n"
            f"{'✅' if pnl > 0 else '❌'} Kâr: {pnl:+.2f} USDC\n"
            f"📊 Toplam PNL: {gs.total_realized_pnl:+.2f} USDC | Döngü: #{gs.grid_cycles}"
        )

        # Aynı seviyeye yeni alış emri koy (döngü devam eder)
        if self.risk.is_trading_allowed():
            rank = self.RANK_MAP.get(pair, 4)
            per_level_usdc = self.capital.get_tier_allocation(rank)["per_level_usdc"]
            new_qty = per_level_usdc / go.level_price
            new_id = await self._place_buy(pair, new_qty, go.level_price, gs)
            if new_id:
                logger.info("GridEngine: %s seviye %.4f için yeni alış koyuldu", pair, go.level_price)

    # ------------------------------------------------------------------
    # Emir işlemleri
    # ------------------------------------------------------------------

    async def _place_buy(
        self, pair: str, qty: float, price: float, gs: GridState
    ) -> Optional[str]:
        """Limit alış emri koy. Order ID veya None döner."""
        try:
            if self.dry_run:
                order_id = f"dry_buy_{pair.replace('/', '')}_{int(price * 100)}"
                go = GridOrder(
                    pair=pair, level_price=price,
                    buy_order_id=order_id, buy_price=price,
                    buy_qty=qty, usdc_allocated=qty * price,
                )
                gs.active_orders[order_id] = go
                logger.info("[DRY] BUY %.6f %s @ %.4f", qty, pair, price)
                return order_id
            else:
                result = await asyncio.to_thread(
                    self.exchange.execute_order, pair, "buy", qty, price, "limit"
                )
                if result is None:
                    return None
                order_id = str(result["id"])
                go = GridOrder(
                    pair=pair, level_price=price,
                    buy_order_id=order_id, buy_price=price,
                    buy_qty=qty, usdc_allocated=qty * price,
                )
                gs.active_orders[order_id] = go
                logger.info("BUY emir koyuldu: %.6f %s @ %.4f [%s]", qty, pair, price, order_id)
                return order_id
        except Exception as e:
            logger.error("GridEngine: BUY emir hatası %s @ %.4f: %s", pair, price, e)
            return None

    async def _place_sell(
        self, pair: str, qty: float, price: float, go: GridOrder, gs: GridState
    ) -> Optional[str]:
        """Limit satış emri koy. Order ID veya None döner."""
        try:
            if self.dry_run:
                sell_id = f"dry_sell_{pair.replace('/', '')}_{int(price * 100)}"
                logger.info("[DRY] SELL %.6f %s @ %.4f", qty, pair, price)
                return sell_id
            else:
                result = await asyncio.to_thread(
                    self.exchange.execute_order, pair, "sell", qty, price, "limit"
                )
                if result is None:
                    return None
                sell_id = str(result["id"])
                logger.info("SELL emir koyuldu: %.6f %s @ %.4f [%s]", qty, pair, price, sell_id)
                return sell_id
        except Exception as e:
            logger.error("GridEngine: SELL emir hatası %s @ %.4f: %s", pair, price, e)
            return None

    # ------------------------------------------------------------------
    # Yardımcı metodlar
    # ------------------------------------------------------------------

    def _calc_sell_price(self, buy_price: float, levels: List[float]) -> float:
        """
        Alış fiyatının üstündeki ilk grid seviyesini satış hedefi olarak kullan.
        Seviye yoksa %2 sabit aralık kullan.
        """
        above = [l for l in sorted(levels) if l > buy_price * (1 + self.GRID_SPACING_BUFFER)]
        if above:
            return above[0]
        return buy_price * 1.02  # Fallback: %2 sabit spacing

    def get_status_text(self) -> str:
        """Telegram için okunabilir grid özeti döner."""
        if not self._grids:
            return "📊 Grid henüz başlatılmadı"

        lines = ["📊 <b>Grid Engine v2 Durumu</b>\n"]
        for pair, gs in self._grids.items():
            pending_buy = sum(1 for o in gs.active_orders.values() if o.state == "PENDING_BUY")
            sell_pending = sum(1 for o in gs.active_orders.values() if o.state == "SELL_PENDING")
            completed = sum(1 for o in gs.active_orders.values() if o.state == "COMPLETED")
            lines.append(
                f"<b>{pair}</b> | Fiyat: {gs.current_price:.4f}\n"
                f"  ⏳ Alış bekleyen: {pending_buy}\n"
                f"  📈 Satış bekleyen: {sell_pending}\n"
                f"  ✅ Tamamlanan: {completed}\n"
                f"  💰 PNL: {gs.total_realized_pnl:+.2f} USDC | Döngü: #{gs.grid_cycles}\n"
            )
        return "\n".join(lines)

    def get_stats(self) -> dict:
        """Günlük rapor için istatistik dict döner."""
        total_pnl = sum(gs.total_realized_pnl for gs in self._grids.values())
        total_cycles = sum(gs.grid_cycles for gs in self._grids.values())
        active_orders = sum(
            1 for gs in self._grids.values()
            for o in gs.active_orders.values()
            if o.state in ("PENDING_BUY", "SELL_PENDING")
        )
        return {
            "total_pnl_usdc": round(total_pnl, 2),
            "total_cycles": total_cycles,
            "active_orders": active_orders,
            "pairs": list(self._grids.keys()),
        }

    # ------------------------------------------------------------------
    # Durum kalıcılığı
    # ------------------------------------------------------------------

    def _save_state(self) -> None:
        """Grid durumunu diske kaydet."""
        try:
            self.DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
            out = {}
            for pair, gs in self._grids.items():
                out[pair] = {
                    "levels": gs.levels,
                    "active_orders": {
                        oid: asdict(o) for oid, o in gs.active_orders.items()
                    },
                    "total_realized_pnl": gs.total_realized_pnl,
                    "grid_cycles": gs.grid_cycles,
                    "last_adapted": gs.last_adapted,
                    "current_price": gs.current_price,
                }
            self.DATA_FILE.write_text(json.dumps(out, indent=2))
        except Exception as e:
            logger.error("GridEngine: durum kaydetme hatası: %s", e)

    def _load_state(self) -> None:
        """Önceki oturumdan grid durumunu yükle."""
        if not self.DATA_FILE.exists():
            return
        try:
            raw = json.loads(self.DATA_FILE.read_text())
            for pair, data in raw.items():
                orders = {}
                for oid, o in data.get("active_orders", {}).items():
                    # COMPLETED emirleri yeniden yükleme (temiz başlangıç)
                    if o.get("state") not in ("COMPLETED", "CANCELLED"):
                        orders[oid] = GridOrder(**o)

                self._grids[pair] = GridState(
                    pair=pair,
                    levels=data["levels"],
                    active_orders=orders,
                    total_realized_pnl=data.get("total_realized_pnl", 0.0),
                    grid_cycles=data.get("grid_cycles", 0),
                    last_adapted=data.get("last_adapted"),
                    current_price=data.get("current_price", 0.0),
                )
            logger.info("GridEngine: önceki durum yüklendi: %s", list(self._grids.keys()))
        except Exception as e:
            logger.error("GridEngine: durum yükleme hatası: %s", e)
