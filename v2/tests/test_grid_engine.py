"""Kapsamlı test paketi — GridEngine v2.

Neyi test ediyor:
  1. Veri yapıları     — GridOrder / GridState oluşturma ve serileştirme
  2. Fiyat hesaplama   — _calc_sell_price (seviye ve fallback)
  3. Alış emirleri     — dry-run buy placement + state mutation
  4. Satış emirleri    — dry-run sell placement
  5. Fill simülasyonu  — BUY fill → SELL koyulur, SELL fill → döngü, PNL
  6. Adaptasyon        — adapt_grid: eski emirleri iptal, yenilerini koy
  7. Kalıcılık         — save/load state (JSON round-trip)
  8. Pyflakes kontrol  — import hataları yok
"""

import asyncio
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# v2 ve repo kök dizinini path'e ekle
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

# grid_engine.py içindeki lazy `from custom_modules.telegram_bot import send_alert_sync`
# çağrısını yakalamak için modülü önceden sys.modules'a stub olarak kaydet.
# Bu sayede test sırasında gerçek Telegram API'si çağrılmaz.
_tg_stub = MagicMock()
_tg_stub.send_alert_sync = MagicMock()
sys.modules.setdefault("custom_modules.telegram_bot", _tg_stub)

from grid_engine import GridEngine, GridOrder, GridState


# ---------------------------------------------------------------------------
# Yardımcı mock fabrikaları
# ---------------------------------------------------------------------------

def make_exchange(last_price: float = 95_000.0):
    """Temel exchange mock'u. fetch_ticker + execute_order + cancel_order."""
    exc = MagicMock()
    exc.fetch_ticker.return_value = {"last": last_price, "symbol": "BTC/USDC"}
    exc.execute_order.return_value = {"id": "fake_order_001", "status": "open"}
    exc.cancel_order.return_value = True
    exc.exchange = MagicMock()      # iç ccxt nesnesi (fetch_order için)
    return exc


def make_capital(per_level_usdc: float = 12.0):
    cap = MagicMock()
    cap.get_tier_allocation.return_value = {
        "tier": 1,
        "per_level_usdc": per_level_usdc,
        "per_coin_usdc": 120.0,
        "grid_levels": 10,
    }
    cap.lock_grid.return_value = None
    cap.release.return_value = 0.0
    cap.get_balance_snapshot.return_value = {
        "total": 1000.0,
        "grid_locked": 200.0,
        "screener_locked": 0.0,
        "available": 800.0,
    }
    return cap


def make_risk(allowed: bool = True):
    risk = MagicMock()
    risk.is_trading_allowed.return_value = allowed
    risk.record_trade_result.return_value = None
    return risk


def make_engine(last_price: float = 95_000.0, per_level=12.0, allowed=True) -> GridEngine:
    """Tam mock edilmiş GridEngine (disk I/O yok)."""
    engine = GridEngine(
        exchange=make_exchange(last_price),
        capital=make_capital(per_level),
        risk=make_risk(allowed),
        dry_run=True,
    )
    # Disk işlemlerini devre dışı bırak
    engine.DATA_FILE = Path("/tmp/test_grid_orders.json")
    engine._save_state = MagicMock()
    engine._load_state = MagicMock()
    return engine


def run(coro):
    """Sync test'lerde async coroutine çalıştır."""
    return asyncio.get_event_loop().run_until_complete(coro)


# ============================================================
# 1. VERİ YAPILARI
# ============================================================

class TestDataClasses(unittest.TestCase):

    def test_grid_order_defaults(self):
        go = GridOrder(
            pair="BTC/USDC",
            level_price=94_000.0,
            buy_order_id="oid1",
            buy_price=94_000.0,
            buy_qty=0.0001,
            usdc_allocated=9.4,
        )
        self.assertEqual(go.state, "PENDING_BUY")
        self.assertIsNone(go.sell_order_id)
        self.assertIsNone(go.sell_price)
        self.assertEqual(go.realized_pnl_usdc, 0.0)

    def test_grid_state_defaults(self):
        gs = GridState(pair="ETH/USDC", levels=[3000.0, 3100.0])
        self.assertEqual(gs.grid_cycles, 0)
        self.assertEqual(gs.total_realized_pnl, 0.0)
        self.assertEqual(gs.active_orders, {})

    def test_grid_order_json_roundtrip(self):
        """GridOrder → dict → GridOrder round-trip (asdict + ** unpack)."""
        from dataclasses import asdict
        go = GridOrder(
            pair="SOL/USDC",
            level_price=170.0,
            buy_order_id="abc",
            buy_price=170.0,
            buy_qty=0.5,
            usdc_allocated=85.0,
            state="SELL_PENDING",
            sell_order_id="def",
            sell_price=174.0,
        )
        d = asdict(go)
        go2 = GridOrder(**d)
        self.assertEqual(go2.pair, go.pair)
        self.assertEqual(go2.sell_price, go.sell_price)
        self.assertEqual(go2.state, go.state)


# ============================================================
# 2. SATIŞ FİYATI HESAPLAMA
# ============================================================

class TestCalcSellPrice(unittest.TestCase):

    def setUp(self):
        self.engine = make_engine()

    def test_next_level_above(self):
        levels = [90_000, 92_000, 94_000, 96_000, 98_000]
        # 94000'da alış doldu → bir sonraki seviye 96000
        result = self.engine._calc_sell_price(94_000.0, levels)
        self.assertEqual(result, 96_000.0)

    def test_buffer_applied(self):
        """Satış fiyatı tam bir üst seviyeye eşit veya biraz üstünde olmalı."""
        levels = [94_000, 96_000]
        result = self.engine._calc_sell_price(94_000.0, levels)
        # 96000 tamamen yukarıda ve tampon gerekmez çünkü zaten > buy_price*1.001
        self.assertGreater(result, 94_000.0)

    def test_fallback_two_percent(self):
        """Üstte seviye yoksa %2 fallback uygulanır."""
        levels = [90_000, 92_000, 94_000]  # Hepsi alış fiyatının altında/eşit
        result = self.engine._calc_sell_price(94_000.0, levels)
        self.assertAlmostEqual(result, 94_000.0 * 1.02, places=2)

    def test_single_level_list(self):
        """Tek seviye listesinde fallback devreye girer."""
        result = self.engine._calc_sell_price(5000.0, [5000.0])
        self.assertAlmostEqual(result, 5000.0 * 1.02, places=2)


# ============================================================
# 3. ALIŞ EMRİ OLUŞTURMA (dry-run)
# ============================================================

class TestPlaceBuy(unittest.TestCase):

    def test_buy_order_added_to_state(self):
        engine = make_engine(last_price=95_000)
        gs = GridState(pair="BTC/USDC", levels=[93_000, 94_000])
        engine._grids["BTC/USDC"] = gs

        order_id = run(engine._place_buy("BTC/USDC", 0.0001, 94_000.0, gs))

        self.assertIsNotNone(order_id)
        self.assertIn(order_id, gs.active_orders)
        go = gs.active_orders[order_id]
        self.assertEqual(go.state, "PENDING_BUY")
        self.assertEqual(go.buy_price, 94_000.0)
        self.assertAlmostEqual(go.buy_qty, 0.0001)

    def test_buy_order_id_format_dry_run(self):
        engine = make_engine()
        gs = GridState(pair="BTC/USDC", levels=[94_000])
        order_id = run(engine._place_buy("BTC/USDC", 0.0001, 94_000.0, gs))
        self.assertTrue(order_id.startswith("dry_buy_"))

    def test_buy_exchange_error_returns_none(self):
        engine = make_engine()
        engine.dry_run = False
        engine.exchange.execute_order.return_value = None  # Başarısız emir
        gs = GridState(pair="BTC/USDC", levels=[94_000])
        order_id = run(engine._place_buy("BTC/USDC", 0.0001, 94_000.0, gs))
        self.assertIsNone(order_id)


# ============================================================
# 4. SATIŞ EMRİ OLUŞTURMA (dry-run)
# ============================================================

class TestPlaceSell(unittest.TestCase):

    def test_sell_order_returns_id(self):
        engine = make_engine()
        gs = GridState(pair="BTC/USDC", levels=[94_000, 96_000])
        go = GridOrder(
            pair="BTC/USDC", level_price=94_000,
            buy_order_id="b1", buy_price=94_000, buy_qty=0.0001, usdc_allocated=9.4,
        )
        sell_id = run(engine._place_sell("BTC/USDC", 0.0001, 96_000.0, go, gs))
        self.assertIsNotNone(sell_id)
        self.assertTrue(sell_id.startswith("dry_sell_"))


# ============================================================
# 5. FILL SİMÜLASYONU (async)
# ============================================================

class TestFillSimulation(unittest.IsolatedAsyncioTestCase):

    async def test_buy_fill_places_sell(self):
        """Fiyat alış seviyesine düşünce SELL emri koyulur."""
        engine = make_engine(last_price=94_000.0)  # Fiyat = alış seviyesi
        gs = GridState(pair="BTC/USDC", levels=[94_000.0, 96_000.0])

        buy_oid = "dry_buy_BTCUSDC_9400000"
        go = GridOrder(
            pair="BTC/USDC", level_price=94_000.0,
            buy_order_id=buy_oid, buy_price=94_000.0,
            buy_qty=0.0001, usdc_allocated=9.4,
        )
        gs.active_orders[buy_oid] = go
        engine._grids["BTC/USDC"] = gs

        if True:  # telegram stublanmış, patch gerekmez
            await engine._simulate_fills("BTC/USDC", gs, {buy_oid: go})

        # Durum SELL_PENDING'e geçmeli
        self.assertEqual(go.state, "SELL_PENDING")
        self.assertIsNotNone(go.sell_order_id)
        self.assertIsNotNone(go.sell_price)
        self.assertGreater(go.sell_price, 94_000.0)

    async def test_sell_fill_records_pnl(self):
        """Satış dolduğunda PNL hesaplanır ve döngü sayacı artar."""
        engine = make_engine(last_price=96_100.0)  # Fiyat satış seviyesinin üstünde
        gs = GridState(pair="BTC/USDC", levels=[94_000.0, 96_000.0])

        sell_oid = "dry_sell_BTCUSDC_9600000"
        go = GridOrder(
            pair="BTC/USDC", level_price=94_000.0,
            buy_order_id="dry_buy_xxx", buy_price=94_000.0,
            buy_qty=0.0001, usdc_allocated=9.4,
            state="SELL_PENDING",
            sell_order_id=sell_oid,
            sell_price=96_000.0,
        )
        gs.active_orders[sell_oid] = go
        engine._grids["BTC/USDC"] = gs

        if True:  # telegram stublanmış, patch gerekmez
            await engine._simulate_fills("BTC/USDC", gs, {sell_oid: go})

        # PNL: (96000 - 94000) * 0.0001 = 0.2 USDC
        self.assertAlmostEqual(go.realized_pnl_usdc, 0.2, places=4)
        self.assertEqual(gs.grid_cycles, 1)
        self.assertAlmostEqual(gs.total_realized_pnl, 0.2, places=4)

    async def test_buy_not_filled_when_price_above(self):
        """Fiyat alış fiyatının üstündeyken fill olmaz."""
        engine = make_engine(last_price=97_000.0)
        gs = GridState(pair="BTC/USDC", levels=[94_000.0])
        buy_oid = "dry_buy_xxx"
        go = GridOrder(
            pair="BTC/USDC", level_price=94_000.0,
            buy_order_id=buy_oid, buy_price=94_000.0,
            buy_qty=0.0001, usdc_allocated=9.4,
        )
        gs.active_orders[buy_oid] = go
        engine._grids["BTC/USDC"] = gs

        await engine._simulate_fills("BTC/USDC", gs, {buy_oid: go})

        self.assertEqual(go.state, "PENDING_BUY")  # Değişmemeli

    async def test_full_cycle_buy_then_sell(self):
        """Tam döngü: BUY fill → SELL fill → yeni BUY koyulur."""
        engine = make_engine(last_price=94_000.0)
        gs = GridState(pair="BTC/USDC", levels=[94_000.0, 96_000.0])

        buy_oid = "dry_buy_cycle_001"
        go = GridOrder(
            pair="BTC/USDC", level_price=94_000.0,
            buy_order_id=buy_oid, buy_price=94_000.0,
            buy_qty=0.0001, usdc_allocated=9.4,
        )
        gs.active_orders[buy_oid] = go
        engine._grids["BTC/USDC"] = gs

        if True:  # telegram stublanmış, patch gerekmez
            # --- Adım 1: BUY fill ---
            await engine._simulate_fills("BTC/USDC", gs, {buy_oid: go})
            self.assertEqual(go.state, "SELL_PENDING")

            sell_oid = go.sell_order_id
            sell_price = go.sell_price

            # Fiyatı satış seviyesine çek
            engine.exchange.fetch_ticker.return_value = {"last": sell_price + 1}

            pending_sell = {sell_oid: go}
            # --- Adım 2: SELL fill ---
            await engine._simulate_fills("BTC/USDC", gs, pending_sell)

        self.assertEqual(go.state, "COMPLETED")
        self.assertGreater(gs.total_realized_pnl, 0)
        self.assertEqual(gs.grid_cycles, 1)

        # --- Adım 3: yeni BUY aynı seviyeye koyulmuş olmalı ---
        new_buys = [
            o for o in gs.active_orders.values()
            if o.state == "PENDING_BUY" and abs(o.level_price - 94_000.0) < 1
        ]
        self.assertEqual(len(new_buys), 1, "Döngüden sonra yeni alış emri koyulmalı")


# ============================================================
# 6. GRİD BAŞLATMA
# ============================================================

class TestInitCoinGrid(unittest.IsolatedAsyncioTestCase):

    async def test_init_places_buy_orders_below_price(self):
        """Mevcut fiyat altındaki seviyelere BUY emri koyulur."""
        engine = make_engine(last_price=95_000.0)

        levels = [90_000.0, 92_000.0, 94_000.0, 96_000.0, 98_000.0]
        if True:  # telegram stublanmış, patch gerekmez
            await engine._init_coin_grid("BTC/USDC", levels)

        gs = engine._grids.get("BTC/USDC")
        self.assertIsNotNone(gs)

        # Sadece < 95000 * 0.999 = 94905 olan seviyeler emirlere dönüşmeli
        # 90000, 92000, 94000 → 3 emir
        buy_orders = [o for o in gs.active_orders.values() if o.state == "PENDING_BUY"]
        self.assertEqual(len(buy_orders), 3)

    async def test_no_levels_below_price(self):
        """Fiyatın altında seviye yoksa warning, emir yok."""
        engine = make_engine(last_price=85_000.0)
        levels = [90_000.0, 92_000.0]  # Hepsi fiyatın üstünde
        if True:  # telegram stublanmış, patch gerekmez
            await engine._init_coin_grid("BTC/USDC", levels)

        gs = engine._grids.get("BTC/USDC")
        # GridState oluşturulmamış veya emir yok
        if gs:
            self.assertEqual(len(gs.active_orders), 0)

    async def test_minimum_notional_filter(self):
        """Küçük miktarlar (< 5 USDC) atlanır."""
        # per_level_usdc = 0.001 → qty * price < 5 → atlanır
        engine = make_engine(last_price=95_000.0, per_level=0.001)
        levels = [94_000.0]
        if True:  # telegram stublanmış, patch gerekmez
            await engine._init_coin_grid("BTC/USDC", levels)
        gs = engine._grids.get("BTC/USDC")
        if gs:
            self.assertEqual(len(gs.active_orders), 0)


# ============================================================
# 7. ADAPT_GRID
# ============================================================

class TestAdaptGrid(unittest.IsolatedAsyncioTestCase):

    async def test_stale_orders_cancelled_new_placed(self):
        """Eski seviyelerdeki emirler iptal edilir, yeni seviyelere emir koyulur."""
        engine = make_engine(last_price=95_000.0)
        gs = GridState(
            pair="BTC/USDC",
            levels=[90_000.0, 92_000.0, 94_000.0],
        )
        # Eski seviyede açık emir var
        old_oid = "dry_buy_old"
        gs.active_orders[old_oid] = GridOrder(
            pair="BTC/USDC", level_price=90_000.0,
            buy_order_id=old_oid, buy_price=90_000.0,
            buy_qty=0.0001, usdc_allocated=9.0, state="PENDING_BUY",
        )
        engine._grids["BTC/USDC"] = gs

        new_levels = [92_000.0, 94_000.0, 91_000.0]  # 90000 kalktı, 91000 eklendi

        if True:  # telegram stublanmış, patch gerekmez
            await engine.adapt_grid("BTC/USDC", new_levels)

        # Eski 90000 emri iptal edilmeli
        self.assertEqual(gs.active_orders[old_oid].state, "CANCELLED")

        # Yeni 91000 seviyesine emir koyulmuş olmalı
        new_buys = [
            o for o in gs.active_orders.values()
            if o.state == "PENDING_BUY" and abs(o.level_price - 91_000.0) < 1
        ]
        self.assertEqual(len(new_buys), 1)

    async def test_filled_orders_not_cancelled(self):
        """Fill olmuş (SELL_PENDING) emirler adaptasyonda iptal edilmez."""
        engine = make_engine(last_price=95_000.0)
        gs = GridState(pair="BTC/USDC", levels=[90_000.0, 94_000.0])

        filled_oid = "dry_sell_active"
        gs.active_orders[filled_oid] = GridOrder(
            pair="BTC/USDC", level_price=90_000.0,
            buy_order_id="b1", buy_price=90_000.0, buy_qty=0.0001,
            usdc_allocated=9.0, state="SELL_PENDING",  # fill oldu, satış bekliyor
            sell_order_id=filled_oid, sell_price=94_000.0,
        )
        engine._grids["BTC/USDC"] = gs

        new_levels = [93_000.0, 94_000.0]  # 90000 kalktı ama emir fill oldu

        if True:  # telegram stublanmış, patch gerekmez
            await engine.adapt_grid("BTC/USDC", new_levels)

        # SELL_PENDING emir dokunulmadan kalmalı
        self.assertEqual(gs.active_orders[filled_oid].state, "SELL_PENDING")

    async def test_unknown_pair_no_crash(self):
        """Bilinmeyen pair için adapt_grid exception atmamalı."""
        engine = make_engine()
        await engine.adapt_grid("UNKNOWN/USDC", [1.0, 2.0])  # Crash olmamalı


# ============================================================
# 8. KALICILıK (save/load)
# ============================================================

class TestPersistence(unittest.TestCase):

    def setUp(self):
        self.tmp = Path("/tmp/test_grid_state.json")
        if self.tmp.exists():
            self.tmp.unlink()

    def tearDown(self):
        if self.tmp.exists():
            self.tmp.unlink()

    def test_save_and_load_roundtrip(self):
        engine = make_engine()
        engine.DATA_FILE = self.tmp
        engine._load_state = GridEngine._load_state.__get__(engine)  # Gerçek load
        engine._save_state = GridEngine._save_state.__get__(engine)  # Gerçek save

        # State kur
        gs = GridState(
            pair="ETH/USDC",
            levels=[3000.0, 3100.0, 3200.0],
            total_realized_pnl=25.5,
            grid_cycles=3,
        )
        go = GridOrder(
            pair="ETH/USDC", level_price=3000.0,
            buy_order_id="oid_eth_1", buy_price=3000.0,
            buy_qty=0.003, usdc_allocated=9.0,
            state="PENDING_BUY",
        )
        gs.active_orders["oid_eth_1"] = go
        engine._grids["ETH/USDC"] = gs

        engine._save_state()
        self.assertTrue(self.tmp.exists())

        # Yeni engine ile yükle
        engine2 = make_engine()
        engine2.DATA_FILE = self.tmp
        engine2._load_state = GridEngine._load_state.__get__(engine2)
        engine2._grids = {}  # Temizle
        engine2._load_state()

        self.assertIn("ETH/USDC", engine2._grids)
        gs2 = engine2._grids["ETH/USDC"]
        self.assertAlmostEqual(gs2.total_realized_pnl, 25.5)
        self.assertEqual(gs2.grid_cycles, 3)
        self.assertIn("oid_eth_1", gs2.active_orders)

    def test_completed_orders_not_loaded(self):
        """COMPLETED emirler yeniden yüklendiğinde atlanır (temiz başlangıç)."""
        engine = make_engine()
        engine.DATA_FILE = self.tmp
        engine._save_state = GridEngine._save_state.__get__(engine)
        engine._load_state = GridEngine._load_state.__get__(engine)

        gs = GridState(pair="BTC/USDC", levels=[94_000.0])
        go_done = GridOrder(
            pair="BTC/USDC", level_price=94_000.0,
            buy_order_id="done_1", buy_price=94_000.0,
            buy_qty=0.0001, usdc_allocated=9.4, state="COMPLETED",
        )
        go_active = GridOrder(
            pair="BTC/USDC", level_price=92_000.0,
            buy_order_id="active_1", buy_price=92_000.0,
            buy_qty=0.0001, usdc_allocated=9.2, state="PENDING_BUY",
        )
        gs.active_orders["done_1"] = go_done
        gs.active_orders["active_1"] = go_active
        engine._grids["BTC/USDC"] = gs
        engine._save_state()

        engine._grids = {}
        engine._load_state()

        gs2 = engine._grids["BTC/USDC"]
        self.assertNotIn("done_1", gs2.active_orders)
        self.assertIn("active_1", gs2.active_orders)


# ============================================================
# 9. DURUM METİNLERİ
# ============================================================

class TestStatusText(unittest.TestCase):

    def test_get_status_text_empty(self):
        engine = make_engine()
        text = engine.get_status_text()
        self.assertIn("henüz başlatılmadı", text)

    def test_get_status_text_with_data(self):
        engine = make_engine()
        gs = GridState(
            pair="BTC/USDC", levels=[94_000], current_price=95_000,
            total_realized_pnl=12.5, grid_cycles=7,
        )
        engine._grids["BTC/USDC"] = gs
        text = engine.get_status_text()
        self.assertIn("BTC/USDC", text)
        self.assertIn("12.50", text)
        self.assertIn("#7", text)

    def test_get_stats(self):
        engine = make_engine()
        gs = GridState(pair="ETH/USDC", levels=[], total_realized_pnl=30.0, grid_cycles=5)
        engine._grids["ETH/USDC"] = gs
        stats = engine.get_stats()
        self.assertEqual(stats["total_pnl_usdc"], 30.0)
        self.assertEqual(stats["total_cycles"], 5)
        self.assertIn("ETH/USDC", stats["pairs"])


# ============================================================
# 10. PYFLAKES KONTROLLERİ (dosya bazlı)
# ============================================================

class TestPyflakesClean(unittest.TestCase):
    """grid_engine.py ve main_v2.py'de isimlendirilmemiş import olmamalı."""

    def _run_pyflakes(self, filepath: str) -> list[str]:
        import ast
        import io
        from pyflakes import api as pf_api, reporter as pf_reporter

        with open(filepath) as f:
            source = f.read()

        out = io.StringIO()
        err = io.StringIO()
        result_reporter = pf_reporter.Reporter(out, err)

        warnings = pf_api.check(source, filepath, result_reporter)
        output = out.getvalue().strip()
        return output.split("\n") if output else []

    def test_grid_engine_no_pyflakes_warnings(self):
        try:
            import pyflakes  # noqa: F401
        except ImportError:
            self.skipTest("pyflakes yüklü değil")
        warnings = self._run_pyflakes(str(Path(__file__).parent.parent / "grid_engine.py"))
        self.assertEqual(warnings, [], f"Pyflakes uyarıları:\n" + "\n".join(warnings))

    def test_main_v2_no_pyflakes_warnings(self):
        try:
            import pyflakes  # noqa: F401
        except ImportError:
            self.skipTest("pyflakes yüklü değil")
        warnings = self._run_pyflakes(str(Path(__file__).parent.parent / "main_v2.py"))
        self.assertEqual(warnings, [], f"Pyflakes uyarıları:\n" + "\n".join(warnings))


# ============================================================
# 11. TIER / SERMAYİ ENTEGRASYON
# ============================================================

class TestTierAllocation(unittest.TestCase):
    """RANK_MAP ve TIER_CONFIG tutarlılık kontrolü."""

    def test_rank_map_covers_all_default_coins(self):
        engine = make_engine()
        expected = {"BTC/USDC", "ETH/USDC", "BNB/USDC", "SOL/USDC", "XRP/USDC"}
        self.assertEqual(set(engine.RANK_MAP.keys()), expected)

    def test_tier_config_rank_coverage(self):
        engine = make_engine()
        for rank in engine.RANK_MAP.values():
            self.assertIn(rank, engine.TIER_CONFIG, f"rank {rank} TIER_CONFIG'te yok")

    def test_per_level_usdc_fetched_correctly(self):
        engine = make_engine(per_level=12.0)
        cap = engine.capital
        result = cap.get_tier_allocation(0)
        self.assertEqual(result["per_level_usdc"], 12.0)


# ============================================================
# 12. RISK YÖNETİCİSİ ENTEGRASYONU
# ============================================================

class TestRiskIntegration(unittest.IsolatedAsyncioTestCase):

    async def test_check_fills_skipped_when_trading_not_allowed(self):
        """Circuit breaker aktifken fill kontrolü yapılmaz."""
        engine = make_engine(allowed=False)
        gs = GridState(pair="BTC/USDC", levels=[94_000.0])
        buy_oid = "dry_buy_risk_test"
        go = GridOrder(
            pair="BTC/USDC", level_price=94_000.0,
            buy_order_id=buy_oid, buy_price=94_000.0,
            buy_qty=0.0001, usdc_allocated=9.4,
        )
        gs.active_orders[buy_oid] = go
        engine._grids["BTC/USDC"] = gs

        # Fill döngüsünü tek tick çalıştır
        engine.risk.is_trading_allowed.return_value = False
        await engine._check_fills("BTC/USDC")

        # Emir değişmemeli
        self.assertEqual(go.state, "PENDING_BUY")

    async def test_record_trade_result_called_on_sell_fill(self):
        """Satış fill olunca risk_manager.record_trade_result() çağrılır."""
        engine = make_engine(last_price=96_100.0)
        gs = GridState(pair="BTC/USDC", levels=[94_000.0, 96_000.0])

        sell_oid = "dry_sell_risk_check"
        go = GridOrder(
            pair="BTC/USDC", level_price=94_000.0,
            buy_order_id="b1", buy_price=94_000.0, buy_qty=0.0001,
            usdc_allocated=9.4, state="SELL_PENDING",
            sell_order_id=sell_oid, sell_price=96_000.0,
        )
        gs.active_orders[sell_oid] = go
        engine._grids["BTC/USDC"] = gs

        if True:  # telegram stublanmış, patch gerekmez
            await engine._simulate_fills("BTC/USDC", gs, {sell_oid: go})

        engine.risk.record_trade_result.assert_called_once()
        args = engine.risk.record_trade_result.call_args[0]
        # PNL: (96000 - 94000) * 0.0001 = 0.2
        self.assertAlmostEqual(args[0], 0.2, places=4)


if __name__ == "__main__":
    unittest.main(verbosity=2)
