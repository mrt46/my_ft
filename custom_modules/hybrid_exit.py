"""Hybrid Exit Strategy — EMA-based + laddered profit-taking for screener trades.

Implements a two-phase exit:
  Phase 1: Sell 40% when price reaches EMA200 (dynamic, updated every 4 h).
  Phase 2: Sell remaining 60% in three laddered limit orders (+15%/+18%/+20%).

A -5% stop-loss order is placed at entry for the full position.
"""

import json
import logging
import time
from pathlib import Path
from typing import TypedDict

import yaml

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# TypedDicts
# ---------------------------------------------------------------------------

class ExitOrder(TypedDict):
    """Single exit order record."""

    order_id: str | None   # Exchange order ID (None until placed)
    pair: str
    side: str              # 'sell'
    amount: float
    price: float
    reason: str            # 'EMA200_TOUCH' | 'LADDER_1-3' | 'STOP_LOSS'
    placed: bool
    filled: bool
    timestamp: float


class ExitPlan(TypedDict):
    """Full exit plan for one screener position."""

    pair: str
    entry_price: float
    total_amount: float
    stop_loss_price: float
    orders: list[ExitOrder]
    ema_last_update: float
    active: bool
    timestamp: float


# ---------------------------------------------------------------------------
# HybridExitManager
# ---------------------------------------------------------------------------

class HybridExitManager:
    """Manages hybrid exit orders for screener positions.

    Order structure for a position:
        1. EMA200 touch  → sell 40%  @ EMA200 * 0.998 (limit, updated 4-hourly)
        2. Ladder 1      → sell 30%  @ entry * 1.15
        3. Ladder 2      → sell 20%  @ entry * 1.18
        4. Ladder 3      → sell 10%  @ entry * 1.20
        5. Stop-loss     → sell 100% @ entry * 0.95 (placed immediately)

    Example:
        mgr = HybridExitManager(exchange_wrapper)
        plan = mgr.setup_hybrid_exit('MATIC/USDC', entry_order)
        mgr.update_ema_orders()   # call every 4 h
    """

    PLANS_FILE = Path(__file__).parent.parent / "data" / "exit_plans.json"

    def __init__(self, exchange_wrapper) -> None:
        """Initialise with a live exchange wrapper.

        Args:
            exchange_wrapper: Instance of ``ResilientExchangeWrapper``.
        """
        self._exchange = exchange_wrapper

        settings_path = Path(__file__).parent.parent / "config" / "settings.yaml"
        with open(settings_path) as fh:
            cfg = yaml.safe_load(fh)

        ex = cfg.get("exit", {})
        self._ema_portion: float = ex.get("ema_portion", 0.40)
        self._ladder = [
            (ex.get("ladder_1_pct", 0.15), ex.get("ladder_1_portion", 0.30)),
            (ex.get("ladder_2_pct", 0.18), ex.get("ladder_2_portion", 0.20)),
            (ex.get("ladder_3_pct", 0.20), ex.get("ladder_3_portion", 0.10)),
        ]
        self._stop_pct: float = abs(ex.get("stop_loss_pct", -0.05))
        self._ema_offset: float = ex.get("ema_order_offset", 0.998)

        self._plans: dict[str, ExitPlan] = {}
        self._load_plans()
        logger.info("HybridExitManager initialised")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def setup_hybrid_exit(self, pair: str, entry_order: dict) -> ExitPlan:
        """Create and place all exit orders for a new screener position.

        Args:
            pair: Trading pair, e.g. ``'MATIC/USDC'``.
            entry_order: Filled order dict returned by ``execute_order()``.

        Returns:
            ExitPlan with all placed order records.
        """
        entry_price = float(entry_order.get("price", entry_order.get("average", 0)))
        total_amount = float(entry_order.get("filled", entry_order.get("amount", 0)))

        if not entry_price or not total_amount:
            raise ValueError(f"Invalid entry order for {pair}: {entry_order}")

        logger.info(
            f"Setting up hybrid exit: {pair} entry={entry_price} amount={total_amount}"
        )

        orders: list[ExitOrder] = []

        # --- Phase 1: EMA200 touch ---
        ema200 = self._get_ema200(pair)
        ema_amount = round(total_amount * self._ema_portion, 8)
        ema_price = round(ema200 * self._ema_offset, 8)
        ema_order = self._place_order(pair, ema_amount, ema_price, "EMA200_TOUCH")
        orders.append(ema_order)

        # --- Phase 2: Laddered sells ---
        for i, (pct, portion) in enumerate(self._ladder, 1):
            lad_amount = round(total_amount * portion, 8)
            lad_price = round(entry_price * (1 + pct), 8)
            lad_order = self._place_order(pair, lad_amount, lad_price, f"LADDER_{i}")
            orders.append(lad_order)

        # --- Stop-loss (OCO-style fallback) ---
        stop_price = round(entry_price * (1 - self._stop_pct), 8)
        stop_order = self._place_stop_loss(pair, total_amount, stop_price)
        orders.append(stop_order)

        plan: ExitPlan = {
            "pair": pair,
            "entry_price": entry_price,
            "total_amount": total_amount,
            "stop_loss_price": stop_price,
            "orders": orders,
            "ema_last_update": time.time(),
            "active": True,
            "timestamp": time.time(),
        }
        self._plans[pair] = plan
        self._save_plans()

        self._alert(
            f"🎯 Exit plan aktif: {pair}\n"
            f"Entry: ${entry_price:.4f}\n"
            f"Stop: ${stop_price:.4f} (-{self._stop_pct*100:.0f}%)\n"
            f"EMA touch: ${ema_price:.4f} (40%)\n"
            f"Ladder: +{self._ladder[0][0]*100:.0f}%/+{self._ladder[1][0]*100:.0f}%"
            f"/+{self._ladder[2][0]*100:.0f}%"
        )
        return plan

    def update_ema_orders(self) -> None:
        """Refresh EMA200 prices for all active exit plans.

        Should be called every 4 hours. Cancels and replaces the EMA
        order only if the price has moved more than 2%.
        """
        for pair, plan in self._plans.items():
            if not plan["active"]:
                continue
            try:
                self._update_ema_for_plan(pair, plan)
            except Exception as exc:
                logger.error(f"EMA update failed for {pair}: {exc}")

        self._save_plans()

    def mark_filled(self, pair: str, reason: str) -> None:
        """Mark an exit order as filled (called from strategy/webhook).

        Args:
            pair: Trading pair.
            reason: Order reason string, e.g. ``'EMA200_TOUCH'``.
        """
        plan = self._plans.get(pair)
        if not plan:
            return

        for order in plan["orders"]:
            if order["reason"] == reason and not order["filled"]:
                order["filled"] = True
                logger.info(f"Exit filled: {pair} {reason}")
                break

        # If all non-stop-loss orders filled → deactivate
        non_stop = [o for o in plan["orders"] if o["reason"] != "STOP_LOSS"]
        if all(o["filled"] for o in non_stop):
            plan["active"] = False
            logger.info(f"All exits filled — plan closed: {pair}")

        self._save_plans()

    def cancel_plan(self, pair: str) -> None:
        """Cancel all open exit orders for a pair (e.g. on stop-loss fill).

        Args:
            pair: Trading pair.
        """
        plan = self._plans.get(pair)
        if not plan:
            return

        for order in plan["orders"]:
            if order["placed"] and not order["filled"] and order["order_id"]:
                self._exchange.cancel_order(order["order_id"], pair)
                order["placed"] = False

        plan["active"] = False
        self._save_plans()
        logger.info(f"Exit plan cancelled: {pair}")

    def get_active_plans(self) -> dict[str, ExitPlan]:
        """Return all currently active exit plans."""
        return {k: v for k, v in self._plans.items() if v["active"]}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _place_order(
        self, pair: str, amount: float, price: float, reason: str
    ) -> ExitOrder:
        """Place a limit sell order and return an ExitOrder record."""
        try:
            result = self._exchange.execute_order(
                pair, "sell", amount, price=price, order_type="limit"
            )
            order_id = result.get("id") if result else None
            placed = result is not None
        except Exception as exc:
            logger.error(f"Failed to place {reason} order for {pair}: {exc}")
            order_id = None
            placed = False

        return ExitOrder(
            order_id=order_id,
            pair=pair,
            side="sell",
            amount=amount,
            price=price,
            reason=reason,
            placed=placed,
            filled=False,
            timestamp=time.time(),
        )

    def _place_stop_loss(self, pair: str, amount: float, stop_price: float) -> ExitOrder:
        """Place a stop-loss market order.

        Uses a limit order slightly below stop price as fallback when
        exchange doesn't support true stop orders in spot mode.
        """
        try:
            result = self._exchange.execute_order(
                pair, "sell", amount, price=stop_price * 0.995, order_type="limit"
            )
            order_id = result.get("id") if result else None
            placed = result is not None
        except Exception as exc:
            logger.error(f"Stop-loss placement failed for {pair}: {exc}")
            order_id = None
            placed = False

        return ExitOrder(
            order_id=order_id,
            pair=pair,
            side="sell",
            amount=amount,
            price=stop_price,
            reason="STOP_LOSS",
            placed=placed,
            filled=False,
            timestamp=time.time(),
        )

    def _update_ema_for_plan(self, pair: str, plan: ExitPlan) -> None:
        """Update EMA200 order for a single plan if price has shifted >2%."""
        current_ema = self._get_ema200(pair)
        new_price = round(current_ema * self._ema_offset, 8)

        ema_orders = [o for o in plan["orders"] if o["reason"] == "EMA200_TOUCH"]
        if not ema_orders:
            return

        ema_order = ema_orders[0]
        if ema_order["filled"]:
            return

        old_price = ema_order["price"]
        price_change_pct = abs(new_price - old_price) / old_price

        if price_change_pct < 0.02:  # < 2% change — skip
            return

        logger.info(
            f"EMA order update: {pair} {old_price:.6f} -> {new_price:.6f} "
            f"(change={price_change_pct*100:.1f}%)"
        )

        # Cancel old
        if ema_order["order_id"]:
            self._exchange.cancel_order(ema_order["order_id"], pair)

        # Place new
        new_order = self._place_order(pair, ema_order["amount"], new_price, "EMA200_TOUCH")
        ema_orders[0].update(new_order)  # in-place update
        plan["ema_last_update"] = time.time()

        self._alert(
            f"🔄 EMA emri güncellendi: {pair}\n"
            f"Eski: ${old_price:.4f} -> Yeni: ${new_price:.4f}"
        )

    def _get_ema200(self, pair: str) -> float:
        """Fetch EMA200 (1D) for a pair using live exchange data."""
        raw = self._exchange.fetch_ohlcv(pair, "1d", limit=250)
        close = [c[4] for c in raw]
        # EMA using pandas-style ewm
        import pandas as pd  # noqa: PLC0415

        ema = pd.Series(close).ewm(span=200, adjust=False).mean()
        return float(ema.iloc[-1])

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load_plans(self) -> None:
        try:
            if self.PLANS_FILE.exists():
                self._plans = json.loads(self.PLANS_FILE.read_text())
        except Exception as exc:
            logger.error(f"Failed to load exit plans: {exc}")

    def _save_plans(self) -> None:
        try:
            self.PLANS_FILE.parent.mkdir(parents=True, exist_ok=True)
            self.PLANS_FILE.write_text(json.dumps(self._plans, indent=2))
        except Exception as exc:
            logger.error(f"Failed to save exit plans: {exc}")

    def _alert(self, message: str) -> None:
        try:
            from custom_modules.telegram_bot import send_alert_sync  # noqa: PLC0415

            send_alert_sync(message)
        except Exception:
            logger.warning(f"Telegram alert skipped: {message}")
