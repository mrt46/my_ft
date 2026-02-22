"""BNB Manager — automatic BNB balance top-up for fee optimisation.

Binance charges lower fees when BNB is used to pay trading fees.
This module monitors the BNB balance and automatically purchases
more BNB when it falls below the configured threshold.
"""

import logging
import time
from pathlib import Path
from typing import TypedDict

import yaml
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

logger = logging.getLogger(__name__)


class BnbStatus(TypedDict):
    """Snapshot of the BNB balance check result."""

    bnb_balance_usdc: float
    triggered: bool
    order: dict | None
    timestamp: float


class BnbManager:
    """Automatic BNB top-up to keep trading fees optimised.

    Binance discounts fees by ~25% when paid in BNB.
    This manager fires a market buy order for BNB whenever the
    wallet's BNB value (in USDC) drops below ``auto_buy_threshold_usdc``.

    Example:
        mgr = BnbManager(exchange_wrapper, capital_manager)
        mgr.check_and_top_up()       # call periodically
    """

    BNB_PAIR = "BNB/USDC"

    def __init__(self, exchange_wrapper, capital_manager, dry_run: bool | None = None) -> None:
        """Initialise with live exchange and capital manager instances.

        Args:
            exchange_wrapper: Instance of ``ResilientExchangeWrapper``.
            capital_manager: Instance of ``CapitalManager``.
            dry_run: If True, simulate BNB checks without placing real orders.
                     Defaults to settings.yaml ``bot.dry_run``.
        """
        self._exchange = exchange_wrapper
        self._capital = capital_manager

        settings_path = Path(__file__).parent.parent / "config" / "settings.yaml"
        with open(settings_path) as fh:
            cfg = yaml.safe_load(fh)

        bnb_cfg = cfg.get("bnb", {})
        self._threshold: float = bnb_cfg.get("auto_buy_threshold_usdc", 1.0)
        self._buy_amount: float = bnb_cfg.get("auto_buy_amount_usdc", 5.0)
        self._check_interval: int = bnb_cfg.get("check_interval_minutes", 15) * 60

        if dry_run is None:
            dry_run = cfg.get("bot", {}).get("dry_run", True)
        self._dry_run: bool = dry_run

        self._last_check: float = 0.0
        logger.info(
            f"BnbManager initialised — threshold={self._threshold} USDC, "
            f"buy={self._buy_amount} USDC, dry_run={self._dry_run}"
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def check_and_top_up(self) -> BnbStatus:
        """Check BNB balance and buy if below threshold.

        Respects the check interval to avoid excessive API calls.

        Returns:
            BnbStatus with current balance, whether a buy was triggered,
            and the order dict if one was placed.
        """
        now = time.time()
        if now - self._last_check < self._check_interval:
            logger.debug("BNB check skipped (interval not elapsed)")
            return {
                "bnb_balance_usdc": -1.0,
                "triggered": False,
                "order": None,
                "timestamp": now,
            }

        self._last_check = now

        try:
            bnb_usdc = self._get_bnb_value_usdc()
        except Exception as exc:
            logger.error(f"BNB balance check failed: {exc}")
            return {
                "bnb_balance_usdc": -1.0,
                "triggered": False,
                "order": None,
                "timestamp": now,
            }

        logger.info(f"BNB balance: {bnb_usdc:.4f} USDC (threshold={self._threshold})")

        if bnb_usdc >= self._threshold:
            return {
                "bnb_balance_usdc": round(bnb_usdc, 4),
                "triggered": False,
                "order": None,
                "timestamp": now,
            }

        # Trigger auto-buy
        logger.warning(
            f"BNB balance {bnb_usdc:.4f} USDC < threshold {self._threshold} USDC"
            + (" — DRY-RUN, simulating buy" if self._dry_run else " — buying …")
        )

        if self._dry_run:
            # In dry-run mode BNB balance is always 0 (simulated) — only log,
            # do NOT send a Telegram alert every 15 minutes to avoid spam.
            logger.info(f"[DRY-RUN] Would buy {self._buy_amount:.2f} USDC of BNB")
            return {
                "bnb_balance_usdc": round(bnb_usdc, 4),
                "triggered": True,
                "order": {"dry_run": True, "amount": self._buy_amount},
                "timestamp": now,
            }

        order = self._buy_bnb()

        return {
            "bnb_balance_usdc": round(bnb_usdc, 4),
            "triggered": True,
            "order": order,
            "timestamp": now,
        }

    def get_bnb_balance_usdc(self) -> float:
        """Return current BNB wallet value in USDC.

        Returns:
            USDC equivalent of held BNB, or 0.0 on error.
        """
        try:
            return self._get_bnb_value_usdc()
        except Exception as exc:
            logger.error(f"get_bnb_balance_usdc failed: {exc}")
            return 0.0

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_bnb_value_usdc(self) -> float:
        """Fetch BNB balance and convert to USDC.

        In dry-run mode returns 0.0 to simulate an empty BNB wallet
        (so the auto-buy logic can be exercised without real API calls).
        """
        if self._dry_run:
            return 0.0

        balance = self._exchange.fetch_balance()
        bnb_amount = float(balance.get("BNB", {}).get("free", 0.0))

        if bnb_amount == 0:
            return 0.0

        ticker = self._exchange.fetch_ticker(self.BNB_PAIR)
        bnb_price = float(ticker.get("last", 0.0))
        return bnb_amount * bnb_price

    def _buy_bnb(self) -> dict | None:
        """Execute a market buy of BNB worth ``self._buy_amount`` USDC.

        Returns:
            Order dict if successful, None otherwise.
        """
        try:
            ticker = self._exchange.fetch_ticker(self.BNB_PAIR)
            bnb_price = float(ticker.get("last", 1.0))
            bnb_qty = self._buy_amount / bnb_price

            if not self._capital.can_open_screener_trade(self._buy_amount):
                logger.warning("Insufficient free capital for BNB top-up — skipping")
                self._alert(
                    f"⚠️ BNB alımı için yeterli bakiye yok ({self._buy_amount} USDC)"
                )
                return None

            order = self._exchange.execute_order(
                self.BNB_PAIR, "buy", bnb_qty, order_type="market"
            )
            if order:
                logger.info(
                    f"BNB bought: {bnb_qty:.4f} BNB @ {bnb_price:.2f} "
                    f"= {self._buy_amount:.2f} USDC"
                )
                self._alert(
                    f"🟢 BNB Auto-buy: {bnb_qty:.4f} BNB @ ${bnb_price:.2f}\n"
                    f"Toplam: {self._buy_amount:.2f} USDC"
                )
            return order

        except Exception as exc:
            logger.error(f"BNB buy failed: {exc}")
            self._alert(f"❌ BNB alımı başarısız: {exc}")
            return None

    def _alert(self, message: str) -> None:
        try:
            from custom_modules.telegram_bot import send_alert_sync  # noqa: PLC0415

            send_alert_sync(message)
        except Exception:
            logger.warning(f"Telegram alert skipped: {message}")
