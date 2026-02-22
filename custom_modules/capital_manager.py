"""Capital Manager — balance allocation and trade gate-keeping.

Tracks all locked capital across grid and screener positions,
enforces priority rules (grid first, screener second), and alerts
when available balance drops below the configured threshold.
"""

import json
import logging
import os
import time
from pathlib import Path
from typing import TypedDict

import yaml
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# TypedDicts
# ---------------------------------------------------------------------------

class PositionSnapshot(TypedDict):
    """Single locked position entry stored in data/positions.json."""

    pair: str
    type: str          # 'grid' | 'screener'
    locked_usdc: float
    entry_price: float
    amount: float
    timestamp: float


class BalanceSnapshot(TypedDict):
    """Real-time balance breakdown."""

    total: float
    grid_locked: float
    screener_locked: float
    available: float
    timestamp: float


# ---------------------------------------------------------------------------
# CapitalManager
# ---------------------------------------------------------------------------

class CapitalManager:
    """Manages capital allocation across grid and screener strategies.

    Priority logic:
        1. Grid trading always receives at least ``grid_min_reserve`` USDC.
        2. Screener trades can only use surplus above the grid reserve.
        3. If a screener order cannot be funded, it is queued until the
           next grid sell event frees capital.

    Example:
        cm = CapitalManager(exchange_wrapper)
        if cm.can_open_screener_trade(100):
            cm.lock_screener(pair='MATIC/USDC', amount_usdc=100, ...)
    """

    POSITIONS_FILE = Path(__file__).parent.parent / "data" / "positions.json"
    QUEUE_FILE = Path(__file__).parent.parent / "data" / "screener_queue.json"

    def __init__(self, exchange_wrapper, dry_run: bool | None = None) -> None:
        """Initialise with a live exchange wrapper.

        Args:
            exchange_wrapper: Instance of ``ResilientExchangeWrapper``.
            dry_run: If True, use simulated balance from settings.yaml instead
                     of fetching real balance from Binance. Defaults to
                     settings.yaml ``bot.dry_run`` value.
        """
        self._exchange = exchange_wrapper
        self._positions: dict[str, PositionSnapshot] = {}
        self._pending_queue: list[dict] = []

        settings_path = Path(__file__).parent.parent / "config" / "settings.yaml"
        with open(settings_path) as fh:
            cfg = yaml.safe_load(fh)

        cap = cfg.get("capital", {})
        self._total_usdc: float = cap.get("total_usdc", 1000.0)
        self._grid_min_reserve: float = cap.get("grid_min_reserve", 600.0)
        self._screener_max: float = cap.get("screener_max_per_position", 100.0)
        self._screener_min: float = cap.get("screener_min_per_position", 20.0)
        self._alert_threshold: float = cap.get("low_balance_alert_threshold", 100.0)
        self._deposit_threshold: float = cap.get("deposit_detection_threshold", 50.0)
        self._last_known_balance: float = 0.0

        # Dry-run: use simulated balance, never call real Binance balance API
        if dry_run is None:
            dry_run = cfg.get("bot", {}).get("dry_run", True)
        self._dry_run: bool = dry_run
        if self._dry_run:
            logger.info(
                f"CapitalManager dry-run mode: simulated balance = {self._total_usdc} USDC"
            )

        self._load_positions()
        logger.info("CapitalManager initialised")

    # ------------------------------------------------------------------
    # Public balance API
    # ------------------------------------------------------------------

    def get_balance_snapshot(self) -> BalanceSnapshot:
        """Return a real-time breakdown of capital allocation.

        In dry-run mode the total balance is taken from ``settings.yaml``
        (``capital.total_usdc``) so no real Binance API call is made and
        no false "low balance" alerts are fired.

        Returns:
            BalanceSnapshot with total, grid_locked, screener_locked, available.
        """
        if self._dry_run:
            # Simulated balance: start with configured total, subtract locked positions
            total = self._total_usdc
        else:
            try:
                raw = self._exchange.fetch_balance()
                # free = unspent USDC; position tracking handles locked capital separately
                total = float(raw.get("USDC", {}).get("free", 0.0))
            except Exception as exc:
                logger.error(f"fetch_balance failed, using cached total: {exc}")
                total = self._total_usdc

        grid_locked = self._locked_by_type("grid")
        screener_locked = self._locked_by_type("screener")
        available = max(total - grid_locked - screener_locked, 0.0)

        snap: BalanceSnapshot = {
            "total": round(total, 2),
            "grid_locked": round(grid_locked, 2),
            "screener_locked": round(screener_locked, 2),
            "available": round(available, 2),
            "timestamp": time.time(),
        }

        logger.debug(
            f"Balance: total={snap['total']} grid={snap['grid_locked']} "
            f"screener={snap['screener_locked']} available={snap['available']}"
        )

        self._check_low_balance(available)
        self._detect_deposit(total)
        return snap

    def check_available_balance(self) -> float:
        """Return spendable USDC (convenience wrapper).

        Side effects:
            - Fires Telegram alert when available < threshold.
            - Detects external deposit and alerts + rebalances.

        Returns:
            Available USDC as a float.
        """
        return self.get_balance_snapshot()["available"]

    # ------------------------------------------------------------------
    # Trade gate-keeping
    # ------------------------------------------------------------------

    def can_open_screener_trade(self, amount_usdc: float) -> bool:
        """Check whether a screener trade of *amount_usdc* can be funded.

        Args:
            amount_usdc: Required USDC for the trade.

        Returns:
            ``True`` if there is sufficient free capital above the grid reserve.
        """
        snap = self.get_balance_snapshot()
        # Grid reserve is sacred; subtract both the min reserve AND current grid locks
        # so screener only gets capital that is genuinely surplus to grid needs.
        surplus = (
            snap["total"]
            - self._grid_min_reserve
            - snap["grid_locked"]
            - snap["screener_locked"]
        )
        can = surplus >= max(amount_usdc, self._screener_min)
        logger.info(
            f"can_open_screener_trade({amount_usdc}): surplus={surplus:.2f} -> {can}"
        )
        return can

    def can_open_grid_trade(self, amount_usdc: float) -> bool:
        """Check whether a new grid allocation can be funded.

        Args:
            amount_usdc: Required USDC for the new grid level.

        Returns:
            ``True`` when grid reserve still covers the request.
        """
        snap = self.get_balance_snapshot()
        grid_surplus = self._grid_min_reserve - snap["grid_locked"]
        can = grid_surplus >= amount_usdc
        logger.info(f"can_open_grid_trade({amount_usdc}): surplus={grid_surplus:.2f} -> {can}")
        return can

    # ------------------------------------------------------------------
    # Position tracking
    # ------------------------------------------------------------------

    def lock_screener(
        self,
        pair: str,
        amount_usdc: float,
        entry_price: float,
        amount_coin: float,
    ) -> None:
        """Register a new screener position as locked capital.

        Args:
            pair: Trading pair, e.g. ``'MATIC/USDC'``.
            amount_usdc: USDC value locked.
            entry_price: Entry price at execution.
            amount_coin: Coin quantity purchased.
        """
        pos: PositionSnapshot = {
            "pair": pair,
            "type": "screener",
            "locked_usdc": amount_usdc,
            "entry_price": entry_price,
            "amount": amount_coin,
            "timestamp": time.time(),
        }
        self._positions[pair] = pos
        self._save_positions()
        logger.info(f"Screener locked: {pair} {amount_usdc:.2f} USDC")

    def lock_grid(
        self,
        pair: str,
        amount_usdc: float,
        entry_price: float,
        amount_coin: float,
    ) -> None:
        """Register a new grid position as locked capital.

        Args:
            pair: Trading pair.
            amount_usdc: USDC value locked.
            entry_price: Grid buy price.
            amount_coin: Coin quantity.
        """
        key = f"grid_{pair}_{int(time.time())}"
        pos: PositionSnapshot = {
            "pair": pair,
            "type": "grid",
            "locked_usdc": amount_usdc,
            "entry_price": entry_price,
            "amount": amount_coin,
            "timestamp": time.time(),
        }
        self._positions[key] = pos
        self._save_positions()
        logger.info(f"Grid locked: {pair} {amount_usdc:.2f} USDC")

    def release(self, pair: str, position_type: str = "screener") -> float:
        """Release locked capital for a closed position.

        Args:
            pair: Trading pair to release.
            position_type: ``'screener'`` or ``'grid'``.

        Returns:
            USDC amount that was unlocked (0 if not found).

        Side effects:
            After releasing, checks the pending queue and fires a
            Telegram alert if a queued screener trade can now be funded.
        """
        # For grid, keys are prefixed with 'grid_'
        keys_to_remove = [
            k for k, v in self._positions.items()
            if v["pair"] == pair and v["type"] == position_type
        ]
        released = sum(self._positions[k]["locked_usdc"] for k in keys_to_remove)
        for k in keys_to_remove:
            del self._positions[k]

        self._save_positions()
        logger.info(f"Released: {pair} ({position_type}) — {released:.2f} USDC")

        self._check_pending_queue()
        return released

    # ------------------------------------------------------------------
    # Pending queue (screener trades waiting for capital)
    # ------------------------------------------------------------------

    def add_to_pending_queue(self, pair: str, amount_usdc: float, score: int) -> None:
        """Queue a screener trade that cannot be funded right now.

        Args:
            pair: Trading pair.
            amount_usdc: Requested USDC.
            score: Screener score (higher = more urgent).
        """
        entry = {
            "pair": pair,
            "amount_usdc": amount_usdc,
            "score": score,
            "queued_at": time.time(),
        }
        self._pending_queue.append(entry)
        self._pending_queue.sort(key=lambda x: x["score"], reverse=True)
        self._save_queue()

        logger.info(f"Queued screener trade: {pair} {amount_usdc} USDC (score={score})")
        self._alert(
            f"⏳ Screener pozisyonu beklemede: {pair}\n"
            f"Gerekli: {amount_usdc} USDC\nGrid satış bekleniyor …"
        )

    def get_pending_queue(self) -> list[dict]:
        """Return the current pending screener queue (sorted by score)."""
        return list(self._pending_queue)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _locked_by_type(self, position_type: str) -> float:
        return sum(
            v["locked_usdc"]
            for v in self._positions.values()
            if v["type"] == position_type
        )

    def _total_locked(self) -> float:
        return sum(v["locked_usdc"] for v in self._positions.values())

    def _check_low_balance(self, available: float) -> None:
        if available < self._alert_threshold:
            logger.warning(f"Low balance: {available:.2f} USDC available")
            self._alert(f"🟡 Düşük bakiye: {available:.2f} USDC kullanılabilir")

    def _detect_deposit(self, current_total: float) -> None:
        if (
            self._last_known_balance > 0
            and current_total > self._last_known_balance + self._deposit_threshold
        ):
            delta = current_total - self._last_known_balance
            logger.info(f"Deposit detected: +{delta:.2f} USDC")
            self._alert(f"💰 +{delta:.2f} USDC deposit algılandı! Bakiye: {current_total:.2f} USDC")
        self._last_known_balance = current_total

    def _check_pending_queue(self) -> None:
        if not self._pending_queue:
            return
        available = self.check_available_balance()
        runnable = [q for q in self._pending_queue if q["amount_usdc"] <= available]
        if runnable:
            best = runnable[0]
            self._alert(
                f"✅ Bekleyen trade artık fonlanabilir: {best['pair']} "
                f"{best['amount_usdc']} USDC (score={best['score']})"
            )

    def _load_positions(self) -> None:
        try:
            if self.POSITIONS_FILE.exists():
                self._positions = json.loads(self.POSITIONS_FILE.read_text())
        except Exception as exc:
            logger.error(f"Failed to load positions: {exc}")
            self._positions = {}

        try:
            if self.QUEUE_FILE.exists():
                self._pending_queue = json.loads(self.QUEUE_FILE.read_text())
        except Exception as exc:
            logger.error(f"Failed to load queue: {exc}")
            self._pending_queue = []

    def _save_positions(self) -> None:
        try:
            self.POSITIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
            self.POSITIONS_FILE.write_text(json.dumps(self._positions, indent=2))
        except Exception as exc:
            logger.error(f"Failed to save positions: {exc}")

    def _save_queue(self) -> None:
        try:
            self.QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)
            self.QUEUE_FILE.write_text(json.dumps(self._pending_queue, indent=2))
        except Exception as exc:
            logger.error(f"Failed to save queue: {exc}")

    def _alert(self, message: str) -> None:
        try:
            from custom_modules.telegram_bot import send_alert_sync  # noqa: PLC0415

            send_alert_sync(message)
        except Exception:
            logger.warning(f"Telegram alert skipped: {message}")

    # ------------------------------------------------------------------
    # Tier-based allocation
    # ------------------------------------------------------------------

    def get_tier_allocation(self, rank: int) -> dict:
        """Calculate tier-based capital allocation for grid trading.

        Tier 1 (Rank 0-2): Large Cap - 40% of grid capital, 10 levels
        Tier 2 (Rank 3-5): Mid Cap - 30% of grid capital, 8 levels
        Tier 3 (Rank 6-9): Small Cap - 20% of grid capital, 6 levels

        Args:
            rank: Volume ranking (0-9)

        Returns:
            Dict with tier, allocation_pct, grid_levels, per_level_usdc
        """
        grid_capital = self._total_usdc * 0.6  # 60% for grid

        if rank < 3:
            tier = 1
            allocation_pct = 0.40
            grid_levels = 10
        elif rank < 6:
            tier = 2
            allocation_pct = 0.30
            grid_levels = 8
        else:
            tier = 3
            allocation_pct = 0.20
            grid_levels = 6

        tier_allocation = grid_capital * allocation_pct
        per_level = tier_allocation / grid_levels

        return {
            "tier": tier,
            "allocation_pct": allocation_pct,
            "grid_levels": grid_levels,
            "per_level_usdc": round(per_level, 2),
            "total_allocation": round(tier_allocation, 2),
        }