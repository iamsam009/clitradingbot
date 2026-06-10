"""
BB Squeeze Breakout Strategy - 15-minute timeframe.

Indicators
----------
- Bollinger Bands (period=20, std_dev=2)
- BB Width = upper - lower
- Squeeze: bb_width == bb_width.rolling(10).min() on last closed candle
- Breakout LONG: squeeze AND close > highest high of last 10 closed candles
  (the 10 candles *before* the breakout candle, to avoid self-comparison)
- SHORT disabled for spot live trading

Exit
----
- Trailing stop only (no take-profit)
- Stop = lowest low of last 5 closed candles (long), only moves UP

All signal calculations use the **last closed** candle (index -2) to prevent
repainting / look-ahead bias.
"""

import logging
from dataclasses import dataclass
from typing import List, Optional

import pandas as pd
import numpy as np

from config import StrategyConfig
from exchange_client import Position

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class BBResult:
    """Snapshot of BB indicator values for a single candle."""
    sma: float
    upper: float
    lower: float
    width: float
    volatility: float


@dataclass
class SignalResult:
    """Result of squeeze + breakout signal detection."""
    signal: str                    # "LONG", "SHORT", or "NONE"
    bb: Optional[BBResult] = None
    candle_close: float = 0.0
    candle_open: float = 0.0
    candle_high: float = 0.0
    candle_low: float = 0.0
    candle_time: str = ""
    squeeze_active: bool = False
    squeeze_width: float = 0.0
    squeeze_min: float = 0.0
    highest_10: float = 0.0
    lowest_5: float = 0.0
    reason: str = ""


# ---------------------------------------------------------------------------
# Strategy class
# ---------------------------------------------------------------------------

class BollingerBandStrategy:
    """
    15-minute Bollinger Squeeze Breakout strategy.

    Entry (LONG)
    ------------
    1. Squeeze fires: ``bb_width`` on the last closed candle equals the
       10-period rolling minimum (within 0.1 % tolerance).
    2. Breakout: close of the last closed candle exceeds the highest high
       of the 10 closed candles *preceding* it.

    Exit
    ----
    Trailing stop at the lowest low of the last 5 closed candles.
    For LONG positions the stop only moves **up** (never down).
    No take-profit target is used.
    """

    # pylint: disable=too-many-instance-attributes

    def __init__(self, cfg: StrategyConfig):
        self.cfg = cfg
        self.df: Optional[pd.DataFrame] = None

        logger.info(
            "BB Squeeze Breakout Strategy initialised | tf=%s bb=(%d,%.1f) "
            "squeeze_lb=%d breakout_lb=%d trail_lb=%d",
            cfg.candle_tf, cfg.bb_period, cfg.bb_stddev,
            cfg.squeeze_lookback, cfg.breakout_lookback, cfg.trailing_lookback,
        )

    # ==================================================================
    # DataFrame preparation
    # ==================================================================

    def prepare_dataframe(self, ohlcv: List[List[float]]) -> pd.DataFrame:
        """
        Convert raw OHLCV data into a timezone-aware DataFrame.

        Expected ``ohlcv`` format (SharkEx)::

            [[timestamp_ms, open, high, low, close, volume], ...]
        """
        if not ohlcv:
            self.df = pd.DataFrame()
            return self.df

        df = pd.DataFrame(
            ohlcv,
            columns=["timestamp", "open", "high", "low", "close", "volume"],
        )
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        df["timestamp"] = df["timestamp"].dt.tz_convert("Asia/Calcutta")
        df.set_index("timestamp", inplace=True)
        df.sort_index(inplace=True)

        # Basic candle helpers
        df["is_green"] = df["close"] >= df["open"]
        df["is_red"] = df["close"] < df["open"]

        self.df = df
        return df

    # ==================================================================
    # Bollinger Bands & squeeze calculations
    # ==================================================================

    def _ensure_indicators(self):
        """Make sure BB columns and squeeze flag are present on the DataFrame."""
        if self.df is None or self.df.empty:
            return
        if "bb_sma" not in self.df.columns:
            self._add_bollinger_bands(self.df)

    def _add_bollinger_bands(self, df: pd.DataFrame):
        """
        Add Bollinger Bands, width, rolling-min-width, and squeeze flag.

        Columns added
        -------------
        * bb_sma
        * bb_std
        * bb_upper
        * bb_lower
        * bb_width
        * bb_pct_b
        * bb_width_roll_min  – 10-period rolling minimum, shifted by 1
        * squeeze            – True when width ≈ rolling minimum
        """
        if len(df) < self.cfg.bb_period:
            logger.warning(
                "Not enough candles for BB period %d (have %d)",
                self.cfg.bb_period, len(df),
            )
            return

        period = self.cfg.bb_period
        stddev = self.cfg.bb_stddev
        squeeze_lb = self.cfg.squeeze_lookback

        df["bb_sma"] = df["close"].rolling(window=period).mean()
        df["bb_std"] = df["close"].rolling(window=period).std(ddof=0)
        df["bb_upper"] = df["bb_sma"] + stddev * df["bb_std"]
        df["bb_lower"] = df["bb_sma"] - stddev * df["bb_std"]
        df["bb_width"] = df["bb_upper"] - df["bb_lower"]
        df["bb_pct_b"] = (df["close"] - df["bb_lower"]) / (df["bb_upper"] - df["bb_lower"])

        # Rolling minimum of bb_width, shifted by 1 so the current candle
        # never compares against itself.
        df["bb_width_roll_min"] = (
            df["bb_width"]
            .rolling(window=squeeze_lb)
            .min()
            .shift(1)
        )

        # Squeeze: width at or extremely near the rolling minimum (0.1 % tolerance)
        df["squeeze"] = df["bb_width"] <= df["bb_width_roll_min"] * 1.001

    # ==================================================================
    # BB snapshot
    # ==================================================================

    def get_latest_bb(self) -> Optional[BBResult]:
        """Return the latest Bollinger Band snapshot (last closed candle)."""
        self._ensure_indicators()
        if self.df is None or len(self.df) < self.cfg.bb_period:
            return None

        row = self.df.iloc[-2] if len(self.df) >= 2 else self.df.iloc[-1]
        if pd.isna(row.get("bb_sma")):
            return None

        return BBResult(
            sma=round(float(row["bb_sma"]), 4),
            upper=round(float(row["bb_upper"]), 4),
            lower=round(float(row["bb_lower"]), 4),
            width=round(float(row["bb_width"]), 4),
            volatility=round(float(row["bb_std"]), 4),
        )

    # ==================================================================
    # Squeeze detection
    # ==================================================================

    def detect_squeeze(self) -> bool:
        """
        Return ``True`` if the **last closed candle** is in a Bollinger
        squeeze (width ≈ 10-period rolling minimum).
        """
        self._ensure_indicators()
        if self.df is None or len(self.df) < self.cfg.bb_period + 2:
            return False
        try:
            return bool(self.df["squeeze"].iloc[-2])
        except (IndexError, KeyError):
            return False

    # ==================================================================
    # Breakout signal
    # ==================================================================

    def get_breakout_signal(self) -> str:
        """
        Check for a breakout from a squeeze.

        Returns
        -------
        str
            ``"LONG"`` if squeeze is active AND the close of the last closed
            candle breaks above the highest high of the 10 closed candles
            *preceding* the breakout candle.  ``"NONE"`` otherwise.
        """
        self._ensure_indicators()
        lb = self.cfg.breakout_lookback
        if self.df is None or len(self.df) < lb + 3:
            return "NONE"

        try:
            if not self.detect_squeeze():
                return "NONE"

            # Highest high of ``lb`` candles BEFORE the last closed candle.
            # Indices: -(lb+2) through -3 (inclusive), which is ``lb`` candles.
            start_idx = -(lb + 2)
            end_idx = -2
            highest_n = self.df["high"].iloc[start_idx:end_idx].max()

            close_last = self.df["close"].iloc[-2]

            if close_last > highest_n:
                return "LONG"
            return "NONE"
        except (IndexError, KeyError):
            return "NONE"

    # ==================================================================
    # Unified entry signal (called by bot.py every cycle)
    # ==================================================================

    def detect_entry_signal(self) -> SignalResult:  # pylint: disable=too-many-locals
        """
        Primary entry-point for the bot.

        Evaluates squeeze + breakout on the last closed candle and returns
        a richly-annotated ``SignalResult`` suitable for logging, dashboard
        display, and trade entry decisions.
        """
        min_candles = self.cfg.bb_period + self.cfg.squeeze_lookback + 2
        if self.df is None or len(self.df) < min_candles:
            return SignalResult(signal="NONE", reason="Insufficient data")

        self._ensure_indicators()

        idx = -2  # last closed candle
        try:
            row = self.df.iloc[idx]
        except IndexError:
            return SignalResult(signal="NONE", reason="No candle data")

        # -- diagnostics --------------------------------------------------
        bb_result = self.get_latest_bb()
        squeeze_active = self.detect_squeeze()
        breakout = self.get_breakout_signal()

        squeeze_width = round(float(row.get("bb_width", 0.0) or 0.0), 6)
        squeeze_min = round(float(row.get("bb_width_roll_min", 0.0) or 0.0), 6)

        lb = self.cfg.breakout_lookback
        highest_10 = 0.0
        if len(self.df) >= lb + 3:
            try:
                highest_10 = round(float(self.df["high"].iloc[-(lb + 2):-2].max()), 4)
            except (IndexError, KeyError):
                pass

        tl = self.cfg.trailing_lookback
        lowest_5 = 0.0
        if len(self.df) >= tl + 2:
            try:
                lowest_5 = round(float(self.df["low"].iloc[-(tl + 1):-1].min()), 4)
            except (IndexError, KeyError):
                pass

        candle_time = str(row.name) if hasattr(row, "name") else ""
        close_val = round(float(row["close"]), 4)

        # -- signal -------------------------------------------------------
        if breakout == "LONG":
            return SignalResult(
                signal="LONG",
                bb=bb_result,
                candle_close=close_val,
                candle_open=round(float(row["open"]), 4),
                candle_high=round(float(row["high"]), 4),
                candle_low=round(float(row["low"]), 4),
                candle_time=candle_time,
                squeeze_active=squeeze_active,
                squeeze_width=squeeze_width,
                squeeze_min=squeeze_min,
                highest_10=highest_10,
                lowest_5=lowest_5,
                reason=(
                    f"Squeeze breakout: close {close_val:.4f} > "
                    f"highest_{lb} {highest_10:.4f}"
                ),
            )

        # -- no signal ----------------------------------------------------
        reason_parts: List[str] = []
        if squeeze_active:
            reason_parts.append(
                f"squeeze=ON (w={squeeze_width:.6f} min={squeeze_min:.6f})"
            )
        else:
            reason_parts.append(
                f"squeeze=OFF (w={squeeze_width:.6f} min={squeeze_min:.6f})"
            )
        reason_parts.append(f"close={close_val:.4f} highest_{lb}={highest_10:.4f}")

        return SignalResult(
            signal="NONE",
            bb=bb_result,
            candle_close=close_val,
            candle_open=round(float(row["open"]), 4),
            candle_high=round(float(row["high"]), 4),
            candle_low=round(float(row["low"]), 4),
            candle_time=candle_time,
            squeeze_active=squeeze_active,
            squeeze_width=squeeze_width,
            squeeze_min=squeeze_min,
            highest_10=highest_10,
            lowest_5=lowest_5,
            reason=" | ".join(reason_parts),
        )

    # ==================================================================
    # Take Profit – NOT USED (squeeze strategy is stop-only)
    # ==================================================================

    def check_take_profit(
        self,
        position: Position,        # pylint: disable=unused-argument
        current_price: float,      # pylint: disable=unused-argument
        entry_signal: Optional[SignalResult] = None,  # pylint: disable=unused-argument
    ) -> bool:
        """
        Always returns ``False``.

        This strategy uses a trailing-stop-only exit – no fixed take-profit
        target is applied.
        """
        return False

    # ==================================================================
    # Initial stop
    # ==================================================================

    def calculate_initial_stop(self, position: Position) -> float:
        """
        Calculate the initial trailing-stop for a newly-opened LONG position.

        Uses the lowest low of the last ``trailing_lookback`` closed candles
        (including the entry candle).  Falls back to 3 % below entry if
        insufficient data is available.
        """
        tl = self.cfg.trailing_lookback
        if self.df is None or len(self.df) < tl + 2:
            return round(position.entry_price * 0.97, 4)

        try:
            stop = float(self.df["low"].iloc[-(tl + 1):-1].min())
            return round(stop, 4)
        except (IndexError, KeyError):
            return round(position.entry_price * 0.97, 4)

    # ==================================================================
    # Trailing stop update
    # ==================================================================

    def update_trailing_stop(
        self, position: Position, current_price: float  # pylint: disable=unused-argument
    ) -> float:
        """
        Update the trailing stop for an open position.

        The new candidate stop is the lowest low of the last
        ``trailing_lookback`` closed candles.

        Rules
        -----
        * LONG  – stop only moves **up** (tightens as price rises).
        * SHORT – stop only moves **down** (disabled in live spot, kept for
          code completeness).

        Returns the (possibly unchanged) stop price.
        """
        tl = self.cfg.trailing_lookback
        if self.df is None or len(self.df) < tl + 2:
            return position.trailing_stop_price or round(position.entry_price * 0.97, 4)

        try:
            candidate = float(self.df["low"].iloc[-(tl + 1):-1].min())
        except (IndexError, KeyError):
            return position.trailing_stop_price or round(position.entry_price * 0.97, 4)

        candidate = round(candidate, 4)
        current_stop = position.trailing_stop_price or 0.0

        if position.side.upper() == "LONG":
            if candidate > current_stop:
                logger.info(
                    "Trailing stop raised: %.4f → %.4f (lowest_%d=%.4f)",
                    current_stop, candidate, tl, candidate,
                )
                return candidate
            return current_stop
        else:
            # SHORT (theoretical)
            if current_stop == 0.0 or candidate < current_stop:
                return candidate
            return current_stop

    # ==================================================================
    # Stop-loss check
    # ==================================================================

    def check_stop_loss(self, position: Position, current_price: float) -> bool:
        """
        Return ``True`` if *current_price* has breached the trailing stop.

        * LONG  – ``current_price <= stop``
        * SHORT – ``current_price >= stop``
        """
        stop = position.trailing_stop_price
        if stop is None or stop <= 0:
            return False

        if position.side.upper() == "LONG":
            return current_price <= stop
        else:
            return current_price >= stop

    # ==================================================================
    # Display helpers
    # ==================================================================

    def get_ohlcv_for_display(self) -> pd.DataFrame:
        """Return the most recent ``display_window`` rows for CLI / dashboard."""
        if self.df is None or self.df.empty:
            return pd.DataFrame()
        return self.df.tail(self.cfg.display_window)