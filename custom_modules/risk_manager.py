"""Risk Manager — circuit breaker and global loss protection.

Monitors daily P&L, consecutive losses, and open position count.
Triggers a configurable circuit-breaker pause when thresholds are
breached, preventing further trading until conditions normalise.
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

class RiskState(TypedDict):
    """Persisted risk state."""

    consecutive_losses: int
    daily_pnl_usdc: float
    daily_start_balance: float
    circuit_breaker_active: bool
    circuit_breaker_until: float     # Unix timestamp
    last_reset_date: str             # 'YYYY-MM-DD'
    paused_reason: str


class HealthReport(TypedDict):
    """Result of a health_check() call."""

    status: str          # 'healthy' | 'degraded' | 'critical'
    circuit_breaker: bool
    consecutive_losses: int
    daily_pnl_pct: float
    open_positions: int
    details: dict


# ---------------------------------------------------------------------------
# RiskManager
# ---------------------------------------------------------------------------

class RiskManager:
    """Circuit-breaker and position limit enforcer.

    Triggers a trading pause when any of these conditions are met:
        - Daily P&L ≤ ``max_daily_loss_pct`` (default -5%)
        - Consecutive losses ≥ ``max_consecutive_losses`` (default 5)
        - Open positions ≥ ``max_open_positions`` (default 15)

    After a pause, trading resumes automatically after
    ``circuit_breaker_cooldown_hours``.

    Example:
        rm = RiskManager(capital_manager)
        rm.record_trade_result(pnl_usdc=-20, pnl_pct=-2.5)
        if not rm.is_trading_allowed():
            logger.warning("Circuit breaker active — skipping trade")
    """

    STATE_FILE = Path(__file__).parent.parent / "data" / "risk_state.json"

    def __init__(self, capital_manager) -> None:
        """Initialise with a live capital manager.

        Args:
            capital_manager: Instance of ``CapitalManager``.
        """
        self._capital = capital_manager

        settings_path = Path(__file__).parent.parent / "config" / "settings.yaml"
        with open(settings_path) as fh:
            cfg = yaml.safe_load(fh)

        risk = cfg.get("risk", {})
        self._max_daily_loss_pct: float = risk.get("max_daily_loss_pct", -5.0)
        self._max_consecutive_losses: int = risk.get("max_consecutive_losses", 5)
        self._max_open_positions: int = risk.get("max_open_positions", 15)
        self._cooldown_hours: float = risk.get("circuit_breaker_cooldown_hours", 4.0)

        self._state: RiskState = self._load_state()
        self._reset_if_new_day()
        logger.info("RiskManager initialised")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def is_trading_allowed(self) -> bool:
        """Check if new trades can be opened.

        Returns:
            ``True`` when trading is allowed, ``False`` when circuit
            breaker is active or position limit is reached.
        """
        self._reset_if_new_day()
        self._check_cooldown_expiry()

        if self._state["circuit_breaker_active"]:
            logger.warning(
                f"Circuit breaker active: {self._state['paused_reason']}"
            )
            return False

        open_count = self._count_open_positions()
        if open_count >= self._max_open_positions:
            logger.warning(f"Position limit reached: {open_count}/{self._max_open_positions}")
            return False

        return True

    def record_trade_result(self, pnl_usdc: float, pnl_pct: float) -> None:
        """Record a completed trade result and check circuit-breaker conditions.

        Args:
            pnl_usdc: Realised P&L in USDC (negative = loss).
            pnl_pct: Realised P&L as percentage (negative = loss).
        """
        self._reset_if_new_day()

        self._state["daily_pnl_usdc"] = round(
            self._state["daily_pnl_usdc"] + pnl_usdc, 2
        )

        if pnl_usdc < 0:
            self._state["consecutive_losses"] += 1
            logger.warning(
                f"Loss recorded: {pnl_usdc:.2f} USDC ({pnl_pct:.2f}%) — "
                f"consecutive={self._state['consecutive_losses']}"
            )
        else:
            self._state["consecutive_losses"] = 0
            logger.info(
                f"Win recorded: {pnl_usdc:+.2f} USDC ({pnl_pct:+.2f}%)"
            )

        self._evaluate_circuit_breaker()
        self._save_state()

    def health_check(self) -> HealthReport:
        """Perform a comprehensive risk health check.

        Returns:
            HealthReport with overall status and metric details.

        Status levels:
            - ``'healthy'``:  All metrics within normal range.
            - ``'degraded'``: Some metrics approaching limits.
            - ``'critical'``: Circuit breaker active or limit exceeded.
        """
        self._reset_if_new_day()
        self._check_cooldown_expiry()

        snap = self._capital.get_balance_snapshot()
        open_count = self._count_open_positions()

        daily_loss_pct = 0.0
        if self._state["daily_start_balance"] > 0:
            daily_pnl = self._state["daily_pnl_usdc"]
            daily_loss_pct = (daily_pnl / self._state["daily_start_balance"]) * 100

        status = "healthy"
        if self._state["circuit_breaker_active"]:
            status = "critical"
        elif (
            self._state["consecutive_losses"] >= self._max_consecutive_losses // 2
            or daily_loss_pct <= self._max_daily_loss_pct / 2
            or open_count >= self._max_open_positions * 0.8
        ):
            status = "degraded"

        report: HealthReport = {
            "status": status,
            "circuit_breaker": self._state["circuit_breaker_active"],
            "consecutive_losses": self._state["consecutive_losses"],
            "daily_pnl_pct": round(daily_loss_pct, 2),
            "open_positions": open_count,
            "details": {
                "daily_pnl_usdc": self._state["daily_pnl_usdc"],
                "max_daily_loss_pct": self._max_daily_loss_pct,
                "max_consecutive_losses": self._max_consecutive_losses,
                "max_open_positions": self._max_open_positions,
                "paused_reason": self._state["paused_reason"],
                "available_usdc": snap["available"],
            },
        }

        logger.debug(f"Risk health check: {status} (CB={self._state['circuit_breaker_active']})")
        return report

    def manually_reset_circuit_breaker(self) -> None:
        """Manually reset the circuit breaker (emergency use only).

        Should only be called after human review of the situation.
        """
        self._state["circuit_breaker_active"] = False
        self._state["paused_reason"] = ""
        self._state["circuit_breaker_until"] = 0.0
        self._save_state()
        logger.warning("Circuit breaker manually reset")
        self._alert("⚠️ Circuit breaker manuel olarak sıfırlandı!")

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _evaluate_circuit_breaker(self) -> None:
        """Check all thresholds and activate circuit breaker if needed."""
        if self._state["circuit_breaker_active"]:
            return

        snap = self._capital.get_balance_snapshot()
        daily_loss_pct = 0.0
        if self._state["daily_start_balance"] > 0:
            daily_loss_pct = (
                self._state["daily_pnl_usdc"] / self._state["daily_start_balance"]
            ) * 100

        reason = None

        if daily_loss_pct <= self._max_daily_loss_pct:
            reason = (
                f"Günlük kayıp limiti aşıldı: {daily_loss_pct:.1f}% "
                f"(limit={self._max_daily_loss_pct}%)"
            )
        elif self._state["consecutive_losses"] >= self._max_consecutive_losses:
            reason = (
                f"Ardışık kayıp limiti aşıldı: "
                f"{self._state['consecutive_losses']}/{self._max_consecutive_losses}"
            )

        if reason:
            self._activate_circuit_breaker(reason)

    def _activate_circuit_breaker(self, reason: str) -> None:
        """Activate circuit breaker for ``cooldown_hours`` duration."""
        cooldown_until = time.time() + self._cooldown_hours * 3600
        self._state["circuit_breaker_active"] = True
        self._state["circuit_breaker_until"] = cooldown_until
        self._state["paused_reason"] = reason

        resume_str = time.strftime("%H:%M UTC", time.gmtime(cooldown_until))
        logger.critical(f"CIRCUIT BREAKER ACTIVE: {reason} — resuming at {resume_str}")
        self._alert(
            f"🚨 CIRCUIT BREAKER AKTİF!\n\n"
            f"Sebep: {reason}\n"
            f"Trading durduruldu.\n"
            f"Devam: {resume_str}"
        )

    def _check_cooldown_expiry(self) -> None:
        """Auto-reset circuit breaker after cooldown period."""
        if (
            self._state["circuit_breaker_active"]
            and time.time() >= self._state["circuit_breaker_until"] > 0
        ):
            self._state["circuit_breaker_active"] = False
            self._state["paused_reason"] = ""
            self._save_state()
            logger.info("Circuit breaker cooldown expired — trading resumed")
            self._alert("🟢 Circuit breaker sona erdi — trading devam ediyor")

    def _reset_if_new_day(self) -> None:
        """Reset daily counters at midnight UTC."""
        today = time.strftime("%Y-%m-%d", time.gmtime())
        if self._state.get("last_reset_date") != today:
            snap = self._capital.get_balance_snapshot()
            start_balance = snap["total"] if snap["total"] > 0 else self._capital._total_usdc
            self._state["consecutive_losses"] = 0
            self._state["daily_pnl_usdc"] = 0.0
            self._state["daily_start_balance"] = start_balance
            self._state["last_reset_date"] = today
            self._save_state()
            logger.info(f"Daily risk counters reset for {today} (start_balance={start_balance:.2f})")

    def _count_open_positions(self) -> int:
        """Count total open positions from capital manager."""
        try:
            return len(self._capital._positions)
        except Exception:
            return 0

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load_state(self) -> RiskState:
        default: RiskState = {
            "consecutive_losses": 0,
            "daily_pnl_usdc": 0.0,
            "daily_start_balance": 0.0,
            "circuit_breaker_active": False,
            "circuit_breaker_until": 0.0,
            "last_reset_date": "",
            "paused_reason": "",
        }
        try:
            if self.STATE_FILE.exists():
                return json.loads(self.STATE_FILE.read_text())
        except Exception as exc:
            logger.error(f"Failed to load risk state: {exc}")
        return default

    def _save_state(self) -> None:
        try:
            self.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            self.STATE_FILE.write_text(json.dumps(self._state, indent=2))
        except Exception as exc:
            logger.error(f"Failed to save risk state: {exc}")

    def _alert(self, message: str) -> None:
        try:
            from custom_modules.telegram_bot import send_alert_sync  # noqa: PLC0415

            send_alert_sync(message)
        except Exception:
            logger.warning(f"Telegram alert skipped: {message}")
