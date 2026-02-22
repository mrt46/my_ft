"""Unit tests for custom_modules.hybrid_exit."""

import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from custom_modules.hybrid_exit import HybridExitManager, ExitPlan, ExitOrder


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def exit_manager(tmp_path):
    """Return a HybridExitManager with mocked exchange."""
    settings = tmp_path / "config" / "settings.yaml"
    settings.parent.mkdir()
    settings.write_text(
        "exit:\n"
        "  ema_portion: 0.40\n"
        "  ladder_1_pct: 0.15\n"
        "  ladder_1_portion: 0.30\n"
        "  ladder_2_pct: 0.18\n"
        "  ladder_2_portion: 0.20\n"
        "  ladder_3_pct: 0.20\n"
        "  ladder_3_portion: 0.10\n"
        "  stop_loss_pct: -0.05\n"
        "  ema_order_offset: 0.998\n"
    )

    mock_exchange = MagicMock()
    plans_file = tmp_path / "exit_plans.json"

    with patch("custom_modules.hybrid_exit.Path") as mock_path:
        mock_path.return_value.__truediv__.return_value = settings
        mock_path.return_value.parent.parent.__truediv__.return_value = settings

        mgr = HybridExitManager.__new__(HybridExitManager)
        mgr._exchange = mock_exchange
        mgr._ema_portion = 0.40
        mgr._ladder = [(0.15, 0.30), (0.18, 0.20), (0.20, 0.10)]
        mgr._stop_pct = 0.05
        mgr._ema_offset = 0.998
        mgr.PLANS_FILE = plans_file
        mgr._plans = {}

        return mgr


# ---------------------------------------------------------------------------
# Setup hybrid exit
# ---------------------------------------------------------------------------

class TestSetupHybridExit:
    def test_creates_exit_plan(self, exit_manager):
        """Should create exit plan with correct structure."""
        entry_order = {
            "price": 100.0,
            "filled": 10.0,
            "amount": 10.0,
        }
        exit_manager._get_ema200 = MagicMock(return_value=120.0)
        exit_manager._place_order = MagicMock(return_value=ExitOrder(
            order_id="test123",
            pair="MATIC/USDC",
            side="sell",
            amount=1.0,
            price=100.0,
            reason="TEST",
            placed=True,
            filled=False,
            timestamp=time.time(),
        ))
        exit_manager._place_stop_loss = MagicMock(return_value=ExitOrder(
            order_id="stop123",
            pair="MATIC/USDC",
            side="sell",
            amount=10.0,
            price=95.0,
            reason="STOP_LOSS",
            placed=True,
            filled=False,
            timestamp=time.time(),
        ))

        plan = exit_manager.setup_hybrid_exit("MATIC/USDC", entry_order)

        assert plan["pair"] == "MATIC/USDC"
        assert plan["entry_price"] == 100.0
        assert plan["total_amount"] == 10.0
        assert len(plan["orders"]) == 5  # EMA + 3 ladders + stop
        assert plan["active"] is True

    def test_calculates_ema_order_correctly(self, exit_manager):
        """EMA order should be 40% of position at EMA200 * 0.998."""
        entry_order = {"price": 100.0, "filled": 10.0}
        exit_manager._get_ema200 = MagicMock(return_value=120.0)

        placed_orders = []
        def capture_order(pair, amount, price, reason):
            order = ExitOrder(
                order_id=f"id_{reason}",
                pair=pair,
                side="sell",
                amount=amount,
                price=price,
                reason=reason,
                placed=True,
                filled=False,
                timestamp=time.time(),
            )
            placed_orders.append(order)
            return order

        exit_manager._place_order = MagicMock(side_effect=capture_order)
        exit_manager._place_stop_loss = MagicMock(return_value=ExitOrder(
            order_id="stop", pair="MATIC/USDC", side="sell", amount=10.0,
            price=95.0, reason="STOP_LOSS", placed=True, filled=False,
            timestamp=time.time(),
        ))

        exit_manager.setup_hybrid_exit("MATIC/USDC", entry_order)

        # Find EMA order
        ema_orders = [o for o in placed_orders if o["reason"] == "EMA200_TOUCH"]
        assert len(ema_orders) == 1
        assert ema_orders[0]["amount"] == 4.0  # 40% of 10
        assert ema_orders[0]["price"] == pytest.approx(119.76, 0.01)  # 120 * 0.998

    def test_calculates_ladder_orders_correctly(self, exit_manager):
        """Ladder orders should be at correct percentages."""
        entry_order = {"price": 100.0, "filled": 10.0}
        exit_manager._get_ema200 = MagicMock(return_value=120.0)

        placed_orders = []
        def capture_order(pair, amount, price, reason):
            order = ExitOrder(
                order_id=f"id_{reason}",
                pair=pair,
                side="sell",
                amount=amount,
                price=price,
                reason=reason,
                placed=True,
                filled=False,
                timestamp=time.time(),
            )
            placed_orders.append(order)
            return order

        exit_manager._place_order = MagicMock(side_effect=capture_order)
        exit_manager._place_stop_loss = MagicMock(return_value=ExitOrder(
            order_id="stop", pair="MATIC/USDC", side="sell", amount=10.0,
            price=95.0, reason="STOP_LOSS", placed=True, filled=False,
            timestamp=time.time(),
        ))

        exit_manager.setup_hybrid_exit("MATIC/USDC", entry_order)

        # Check ladder orders
        ladder1 = [o for o in placed_orders if o["reason"] == "LADDER_1"][0]
        ladder2 = [o for o in placed_orders if o["reason"] == "LADDER_2"][0]
        ladder3 = [o for o in placed_orders if o["reason"] == "LADDER_3"][0]

        assert ladder1["amount"] == 3.0  # 30% of 10
        assert ladder1["price"] == pytest.approx(115.0, 0.01)  # +15%

        assert ladder2["amount"] == 2.0  # 20% of 10
        assert ladder2["price"] == pytest.approx(118.0, 0.01)  # +18%

        assert ladder3["amount"] == 1.0  # 10% of 10
        assert ladder3["price"] == pytest.approx(120.0, 0.01)  # +20%

    def test_calculates_stop_loss_correctly(self, exit_manager):
        """Stop loss should be at -5% from entry."""
        entry_order = {"price": 100.0, "filled": 10.0}
        exit_manager._get_ema200 = MagicMock(return_value=120.0)
        exit_manager._place_order = MagicMock(return_value=ExitOrder(
            order_id="test", pair="MATIC/USDC", side="sell", amount=1.0,
            price=100.0, reason="TEST", placed=True, filled=False,
            timestamp=time.time(),
        ))

        stop_order = ExitOrder(
            order_id="stop123",
            pair="MATIC/USDC",
            side="sell",
            amount=10.0,
            price=95.0,
            reason="STOP_LOSS",
            placed=True,
            filled=False,
            timestamp=time.time(),
        )
        exit_manager._place_stop_loss = MagicMock(return_value=stop_order)

        plan = exit_manager.setup_hybrid_exit("MATIC/USDC", entry_order)

        stop_orders = [o for o in plan["orders"] if o["reason"] == "STOP_LOSS"]
        assert len(stop_orders) == 1
        assert stop_orders[0]["price"] == 95.0  # -5%


# ---------------------------------------------------------------------------
# Order management
# ---------------------------------------------------------------------------

class TestOrderManagement:
    def test_mark_filled_updates_order(self, exit_manager):
        """Marking an order as filled should update state."""
        exit_manager._plans["MATIC/USDC"] = ExitPlan(
            pair="MATIC/USDC",
            entry_price=100.0,
            total_amount=10.0,
            stop_loss_price=95.0,
            orders=[
                ExitOrder(
                    order_id="ema123",
                    pair="MATIC/USDC",
                    side="sell",
                    amount=4.0,
                    price=119.76,
                    reason="EMA200_TOUCH",
                    placed=True,
                    filled=False,
                    timestamp=time.time(),
                ),
            ],
            ema_last_update=time.time(),
            active=True,
            timestamp=time.time(),
        )

        exit_manager.mark_filled("MATIC/USDC", "EMA200_TOUCH")

        plan = exit_manager._plans["MATIC/USDC"]
        ema_order = [o for o in plan["orders"] if o["reason"] == "EMA200_TOUCH"][0]
        assert ema_order["filled"] is True

    def test_deactivates_when_all_non_stop_orders_filled(self, exit_manager):
        """Should deactivate plan when all take-profit orders filled."""
        exit_manager._plans["MATIC/USDC"] = ExitPlan(
            pair="MATIC/USDC",
            entry_price=100.0,
            total_amount=10.0,
            stop_loss_price=95.0,
            orders=[
                ExitOrder(order_id="ema123", pair="MATIC/USDC", side="sell",
                         amount=4.0, price=119.76, reason="EMA200_TOUCH",
                         placed=True, filled=True, timestamp=time.time()),
                ExitOrder(order_id="lad1", pair="MATIC/USDC", side="sell",
                         amount=3.0, price=115.0, reason="LADDER_1",
                         placed=True, filled=True, timestamp=time.time()),
                ExitOrder(order_id="lad2", pair="MATIC/USDC", side="sell",
                         amount=2.0, price=118.0, reason="LADDER_2",
                         placed=True, filled=True, timestamp=time.time()),
                ExitOrder(order_id="lad3", pair="MATIC/USDC", side="sell",
                         amount=1.0, price=120.0, reason="LADDER_3",
                         placed=True, filled=True, timestamp=time.time()),
                ExitOrder(order_id="stop123", pair="MATIC/USDC", side="sell",
                         amount=10.0, price=95.0, reason="STOP_LOSS",
                         placed=True, filled=False, timestamp=time.time()),
            ],
            ema_last_update=time.time(),
            active=True,
            timestamp=time.time(),
        )

        exit_manager.mark_filled("MATIC/USDC", "LADDER_3")

        assert exit_manager._plans["MATIC/USDC"]["active"] is False

    def test_cancel_plan_cancels_orders(self, exit_manager):
        """Cancel should cancel all open orders."""
        exit_manager._exchange.cancel_order = MagicMock(return_value=True)

        exit_manager._plans["MATIC/USDC"] = ExitPlan(
            pair="MATIC/USDC",
            entry_price=100.0,
            total_amount=10.0,
            stop_loss_price=95.0,
            orders=[
                ExitOrder(order_id="ema123", pair="MATIC/USDC", side="sell",
                         amount=4.0, price=119.76, reason="EMA200_TOUCH",
                         placed=True, filled=False, timestamp=time.time()),
            ],
            ema_last_update=time.time(),
            active=True,
            timestamp=time.time(),
        )

        exit_manager.cancel_plan("MATIC/USDC")

        exit_manager._exchange.cancel_order.assert_called_once_with("ema123", "MATIC/USDC")
        assert exit_manager._plans["MATIC/USDC"]["active"] is False


# ---------------------------------------------------------------------------
# EMA updates
# ---------------------------------------------------------------------------

class TestEmaUpdates:
    def test_updates_ema_order_when_price_changes_significantly(self, exit_manager):
        """Should update EMA order when price changes > 2%."""
        exit_manager._exchange.cancel_order = MagicMock(return_value=True)
        exit_manager._place_order = MagicMock(return_value=ExitOrder(
            order_id="new_ema",
            pair="MATIC/USDC",
            side="sell",
            amount=4.0,
            price=125.0,
            reason="EMA200_TOUCH",
            placed=True,
            filled=False,
            timestamp=time.time(),
        ))

        exit_manager._plans["MATIC/USDC"] = ExitPlan(
            pair="MATIC/USDC",
            entry_price=100.0,
            total_amount=10.0,
            stop_loss_price=95.0,
            orders=[
                ExitOrder(order_id="old_ema", pair="MATIC/USDC", side="sell",
                         amount=4.0, price=119.76, reason="EMA200_TOUCH",
                         placed=True, filled=False, timestamp=time.time()),
            ],
            ema_last_update=time.time(),
            active=True,
            timestamp=time.time(),
        )

        # EMA changed from 120 to 130 (> 2% change)
        exit_manager._get_ema200 = MagicMock(return_value=130.0)

        exit_manager._update_ema_for_plan("MATIC/USDC", exit_manager._plans["MATIC/USDC"])

        # Old order should be cancelled
        exit_manager._exchange.cancel_order.assert_called_once_with("old_ema", "MATIC/USDC")
        # New order should be placed
        exit_manager._place_order.assert_called_once()

    def test_skips_update_when_small_price_change(self, exit_manager):
        """Should skip update when price change < 2%."""
        exit_manager._exchange.cancel_order = MagicMock()
        exit_manager._place_order = MagicMock()

        exit_manager._plans["MATIC/USDC"] = ExitPlan(
            pair="MATIC/USDC",
            entry_price=100.0,
            total_amount=10.0,
            stop_loss_price=95.0,
            orders=[
                ExitOrder(order_id="ema123", pair="MATIC/USDC", side="sell",
                         amount=4.0, price=119.76, reason="EMA200_TOUCH",
                         placed=True, filled=False, timestamp=time.time()),
            ],
            ema_last_update=time.time(),
            active=True,
            timestamp=time.time(),
        )

        # EMA changed slightly (120 -> 120.5, < 2%)
        exit_manager._get_ema200 = MagicMock(return_value=120.5)

        exit_manager._update_ema_for_plan("MATIC/USDC", exit_manager._plans["MATIC/USDC"])

        # No cancel or place should happen
        exit_manager._exchange.cancel_order.assert_not_called()
        exit_manager._place_order.assert_not_called()


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

class TestPersistence:
    def test_saves_plans(self, exit_manager):
        """Should save plans to file."""
        exit_manager._plans["MATIC/USDC"] = ExitPlan(
            pair="MATIC/USDC",
            entry_price=100.0,
            total_amount=10.0,
            stop_loss_price=95.0,
            orders=[],
            ema_last_update=time.time(),
            active=True,
            timestamp=time.time(),
        )

        exit_manager._save_plans()

        assert exit_manager.PLANS_FILE.exists()

    def test_loads_plans(self, exit_manager):
        """Should load plans from file."""
        saved_plan = {
            "MATIC/USDC": {
                "pair": "MATIC/USDC",
                "entry_price": 100.0,
                "total_amount": 10.0,
                "stop_loss_price": 95.0,
                "orders": [],
                "ema_last_update": time.time(),
                "active": True,
                "timestamp": time.time(),
            }
        }
        exit_manager.PLANS_FILE.parent.mkdir(parents=True, exist_ok=True)
        exit_manager.PLANS_FILE.write_text(json.dumps(saved_plan))

        exit_manager._load_plans()

        assert "MATIC/USDC" in exit_manager._plans
        assert exit_manager._plans["MATIC/USDC"]["entry_price"] == 100.0
