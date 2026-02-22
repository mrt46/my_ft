"""Grid Fusion — merges technical grid levels with AI sentiment adjustments.

Reads ``data/base_grid.json`` (from grid_analyzer) and
``data/sentiment_scores.json`` (from sentiment_analyzer), then produces
``data/final_grid.json`` which is the input consumed by Freqtrade's
DynamicGridStrategy.
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

class FusedGrid(TypedDict):
    """Final merged grid ready for Freqtrade."""

    pair: str
    levels: list[float]
    upper_bound: float
    lower_bound: float
    position_size: float
    sentiment_applied: bool
    sentiment_score: float
    sentiment_shift_pct: float   # How much levels shifted due to sentiment
    spacing: str
    timestamp: float


# ---------------------------------------------------------------------------
# GridFusion
# ---------------------------------------------------------------------------

class GridFusion:
    """Merges technical S/R grid with sentiment-driven level adjustments.

    Fusion logic:
        - If sentiment is UNUSABLE (low confidence / too few LLMs):
            Use raw technical grid unchanged.
        - If sentiment is BULLISH (+0.3 … +1.0):
            Shift grid levels UP by up to +3% (proportional to sentiment).
        - If sentiment is BEARISH (-0.3 … -1.0):
            Shift grid levels DOWN by up to -3%.
        - If sentiment is NEUTRAL (-0.3 … +0.3):
            Keep technical grid, minor ±0.5% dither.

    Output ``data/final_grid.json`` is consumed by DynamicGridStrategy.

    Example:
        fusion = GridFusion()
        grids = fusion.run()
    """

    BASE_GRID_FILE = Path(__file__).parent.parent / "data" / "base_grid.json"
    SENTIMENT_FILE = Path(__file__).parent.parent / "data" / "sentiment_scores.json"
    FINAL_GRID_FILE = Path(__file__).parent.parent / "data" / "final_grid.json"

    # Maximum level shift from sentiment (±3%)
    MAX_SHIFT = 0.03
    NEUTRAL_THRESHOLD = 0.3

    def __init__(self) -> None:
        """Load settings."""
        settings_path = Path(__file__).parent.parent / "config" / "settings.yaml"
        with open(settings_path) as fh:
            cfg = yaml.safe_load(fh)

        self._dry_run: bool = cfg.get("bot", {}).get("dry_run", True)
        logger.info("GridFusion initialised")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(self) -> dict[str, FusedGrid]:
        """Run fusion for all pairs present in ``base_grid.json``.

        Returns:
            Mapping of pair → FusedGrid.
        """
        base_grids = self._load_base_grids()
        sentiments = self._load_sentiments()

        fused: dict[str, FusedGrid] = {}
        for pair, grid in base_grids.items():
            coin = pair.split("/")[0]
            sentiment = sentiments.get(coin, {})
            fused[pair] = self._fuse(pair, grid, sentiment)

        self._save(fused)
        logger.info(f"GridFusion complete: {len(fused)} pairs")
        return fused

    def fuse_pair(self, pair: str, grid: dict, sentiment: dict) -> FusedGrid:
        """Fuse a single pair's grid with its sentiment data.

        Args:
            pair: Trading pair, e.g. ``'BTC/USDC'``.
            grid: GridConfig dict from grid_analyzer.
            sentiment: SentimentResult dict from sentiment_analyzer.

        Returns:
            FusedGrid ready for Freqtrade.
        """
        return self._fuse(pair, grid, sentiment)

    # ------------------------------------------------------------------
    # Fusion logic
    # ------------------------------------------------------------------

    def _fuse(self, pair: str, grid: dict, sentiment: dict) -> FusedGrid:
        """Apply sentiment shift to technical grid levels.

        Args:
            pair: Trading pair.
            grid: Technical grid (from GridAnalyzer).
            sentiment: Sentiment result (from SentimentAnalyzer).

        Returns:
            FusedGrid with shifted levels and metadata.
        """
        raw_levels: list[float] = grid.get("levels", [])
        sent_score: float = sentiment.get("sentiment", 0.0)
        sent_usable: bool = sentiment.get("usable", False)

        if not sent_usable:
            logger.info(f"{pair}: sentiment unusable - using raw technical grid")
            shift_pct = 0.0
        elif abs(sent_score) < self.NEUTRAL_THRESHOLD:
            # Neutral — tiny dither
            shift_pct = sent_score * 0.005 / self.NEUTRAL_THRESHOLD
        else:
            # Scale linearly: ±NEUTRAL → 0%, ±1.0 → ±MAX_SHIFT
            shift_pct = (abs(sent_score) - self.NEUTRAL_THRESHOLD) / (1 - self.NEUTRAL_THRESHOLD)
            shift_pct = shift_pct * self.MAX_SHIFT * (1 if sent_score > 0 else -1)

        shifted = [round(lvl * (1 + shift_pct), 6) for lvl in raw_levels]

        fused: FusedGrid = {
            "pair": pair,
            "levels": sorted(shifted),
            "upper_bound": round(grid.get("upper_bound", max(shifted, default=0)) * (1 + shift_pct), 6),
            "lower_bound": round(grid.get("lower_bound", min(shifted, default=0)) * (1 + shift_pct), 6),
            "position_size": grid.get("position_size", 10.0),
            "sentiment_applied": sent_usable,
            "sentiment_score": round(sent_score, 4),
            "sentiment_shift_pct": round(shift_pct * 100, 3),
            "spacing": grid.get("spacing", "fibonacci"),
            "timestamp": time.time(),
        }

        logger.debug(
            f"{pair}: sentiment={sent_score:+.3f} shift={shift_pct*100:+.2f}% "
            f"levels={len(shifted)}"
        )
        return fused

    # ------------------------------------------------------------------
    # I/O helpers
    # ------------------------------------------------------------------

    def _load_base_grids(self) -> dict:
        try:
            return json.loads(self.BASE_GRID_FILE.read_text())
        except FileNotFoundError:
            logger.error("base_grid.json not found — run GridAnalyzer first")
            return {}
        except Exception as exc:
            logger.error(f"Failed to load base_grid.json: {exc}")
            return {}

    def _load_sentiments(self) -> dict:
        try:
            return json.loads(self.SENTIMENT_FILE.read_text())
        except FileNotFoundError:
            logger.warning("sentiment_scores.json not found — proceeding without sentiment")
            return {}
        except Exception as exc:
            logger.error(f"Failed to load sentiment_scores.json: {exc}")
            return {}

    def _save(self, fused: dict[str, FusedGrid]) -> None:
        try:
            self.FINAL_GRID_FILE.parent.mkdir(parents=True, exist_ok=True)
            self.FINAL_GRID_FILE.write_text(json.dumps(fused, indent=2))
            logger.info(f"Final grid saved: {self.FINAL_GRID_FILE}")
        except Exception as exc:
            logger.error(f"Failed to save final_grid.json: {exc}")
