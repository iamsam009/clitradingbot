"""
strategy.py - Bollinger Band Reversal Strategy

Calculates Bollinger Bands, detects entry signals, and manages
trailing stop logic. All calculations use closed 5-minute candles only.
"""

import logging
import math
from typing import List, Optional, Tuple
from dataclasses import dataclass

import numpy as np
import pandas as pd

from config import StrategyConfig
from exchange_client import Position, OrderSide

logger = logging.getLogger("strategy")


@dataclass
class BBResult:
    """Result of Bollinger Band calculation."""
    sma: float          # Middle band
    upper: float        # Upper band
    lower: float        # Lower band
    width: float        # Band width (upper - lower)
    volatility: float   # Standard deviation of price


@dataclass
class SignalResult:
    """Entry signal detection result."""
    signal: str         # "LONG", "SHORT", or "NONE"
    candle_close: float
    candle_open: float
    candle_high: float
    candle_low: float
    candle_time: float
    bb: BBResult
    near_distance_pct: float  # How close to the band (%)
    reason: str = ""


class BollingerBandStrategy:
    """
    Bollinger Band mean-reversion strategy for 5-minute timeframe.
    
    Long Entry: Green candle closes near lower band
    Short Entry: Red candle closes near upper band
    
    Exit:
      - Take-profit: Price touches opposite band
      - Trailing stop: Dynamic stop based on highest/lowest price
    """

    def __init__(self, cfg: StrategyConfig):
        self.cfg = cfg
        self._df: Optional[pd.DataFrame] = None
        self._last_bb: Optional[BBResult] = None

    def prepare_dataframe(self, ohlcv: List[List[float]]) -> pd.DataFrame:
        """
        Convert raw OHLCV data to a pandas DataFrame and calculate indicators.
        
        Args:
            ohlcv: List of [timestamp, open, high, low, close, volume]
        
        Returns:
            DataFrame with BB columns added
        """
        if not ohlcv:
            logger.warning("Empty OHLCV data received")
            return pd.DataFrame()

        df = pd.DataFrame(
            ohlcv,
            columns=["timestamp", "open", "high", "low", "close", "volume"]
        )
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
        df["timestamp_ist"] = df["timestamp"].dt.tz_localize("UTC").dt.tz_convert("Asia/Kolkata")

        # Ensure numeric types
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")

        # Drop any rows with NaN
        df = df.dropna(subset=["open", "high", "low", "close"])

        # Calculate Bollinger Bands
        self._add_bollinger_bands(df)

        self._df = df
        return df

    def _add_bollinger_bands(self, df: pd.DataFrame):
        """Add Bollinger Band columns to the DataFrame."""
        period = self.cfg.bb_period
        std_dev = self.cfg.bb_std_dev

        # Middle band = 20-period SMA
        df["bb_sma"] = df["close"].rolling(window=period).mean()
        
        # Standard deviation
        df["bb_std"] = df["close"].rolling(window=period).std()
        
        # Upper and lower bands
        df["bb_upper"] = df["bb_sma"] + (std_dev * df["bb_std"])
        df["bb_lower"] = df["bb_sma"] - (std_dev * df["bb_std"])
        
        # Band width
        df["bb_width"] = df["bb_upper"] - df["bb_lower"]
        
        # Percentage B (%B) - where price is within the bands
        df["bb_pct_b"] = (df["close"] - df["bb_lower"]) / (df["bb_upper"] - df["bb_lower"])

        # Candle color
        df["is_green"] = df["close"] > df["open"]
        df["is_red"] = df["close"] < df["open"]

    def get_latest_bb(self) -> Optional[BBResult]:
        """Get the latest Bollinger Band values."""
        if self._df is None or len(self._df) < self.cfg.bb_period:
            return None

        latest = self._df.iloc[-1]
        
        sma = float(latest.get("bb_sma", 0))
        upper = float(latest.get("bb_upper", 0))
        lower = float(latest.get("bb_lower", 0))
        width = float(latest.get("bb_width", 0))
        std = float(latest.get("bb_std", 0))

        if not (sma and upper and lower):
            return None

        self._last_bb = BBResult(
            sma=sma,
            upper=upper,
            lower=lower,
            width=width,
            volatility=std,
        )
        return self._last_bb

    def detect_entry_signal(self) -> SignalResult:
        """
        Detect entry signal from the last *fully closed* candle.
        
        Uses candle at index -2 (the candle before the current forming candle).
        This prevents repainting and ensures only completed candles are analyzed.
        
        Returns:
            SignalResult with signal type and details
        """
        no_signal = SignalResult(
            signal="NONE",
            candle_close=0,
            candle_open=0,
            candle_high=0,
            candle_low=0,
            candle_time=0,
            bb=BBResult(0, 0, 0, 0, 0),
            near_distance_pct=0,
            reason="No data available",
        )

        if self._df is None or len(self._df) < self.cfg.bb_period + 2:
            no_signal.reason = f"Insufficient candles (need {self.cfg.bb_period + 2}, have {len(self._df) if self._df is not None else 0})"
            return no_signal

        # Use the second-to-last candle (index -2) = last fully closed candle
        # Index -1 is the currently forming candle
        candle = self._df.iloc[-2]
        bb = self._df.iloc[-2]

        close = float(candle["close"])
        open_p = float(candle["open"])
        high = float(candle["high"])
        low = float(candle["low"])
        upper = float(bb["bb_upper"])
        lower = float(bb["bb_lower"])
        sma = float(bb["bb_sma"])
        bb_std = float(bb["bb_std"])

        # Safety check
        if any(math.isnan(x) or math.isinf(x) for x in [close, open_p, upper, lower, sma]):
            no_signal.reason = "NaN/Inf values in BB calculation"
            return no_signal
        if upper == 0 or lower == 0:
            no_signal.reason = "BB values are zero"
            return no_signal

        threshold = self.cfg.near_threshold

        # ---- LONG SIGNAL ----
        if close > open_p:  # Green candle
            if close > lower:  # Close is above lower band (not below)
                # Calculate how close: (close - lower) / close
                distance_pct = (close - lower) / close
                if distance_pct < threshold:
                    return SignalResult(
                        signal="LONG",
                        candle_close=close,
                        candle_open=open_p,
                        candle_high=high,
                        candle_low=low,
                        candle_time=float(candle["timestamp"].timestamp()),
                        bb=BBResult(sma=sma, upper=upper, lower=lower,
                                    width=upper - lower, volatility=bb_std),
                        near_distance_pct=distance_pct * 100,
                        reason=f"Green candle near lower band. Distance: {distance_pct*100:.3f}% < {threshold*100:.2f}%",
                    )

        # ---- SHORT SIGNAL ----
        if close < open_p:  # Red candle
            if close < upper:  # Close is below upper band
                # Calculate how close: (upper - close) / close
                distance_pct = (upper - close) / close
                if distance_pct < threshold:
                    return SignalResult(
                        signal="SHORT",
                        candle_close=close,
                        candle_open=open_p,
                        candle_high=high,
                        candle_low=low,
                        candle_time=float(candle["timestamp"].timestamp()),
                        bb=BBResult(sma=sma, upper=upper, lower=lower,
                                    width=upper - lower, volatility=bb_std),
                        near_distance_pct=distance_pct * 100,
                        reason=f"Red candle near upper band. Distance: {distance_pct*100:.3f}% < {threshold*100:.2f}%",
                    )

        return SignalResult(
            signal="NONE",
            candle_close=close,
            candle_open=open_p,
            candle_high=high,
            candle_low=low,
            candle_time=float(candle["timestamp"].timestamp()),
            bb=BBResult(sma=sma, upper=upper, lower=lower,
                        width=upper - lower, volatility=bb_std),
            near_distance_pct=0,
            reason="No signal conditions met",
        )

    def check_take_profit(self, position: Position, current_price: float,
                          bb: BBResult) -> bool:
        """
        Check if take-profit condition is met.
        
        Long TP: Price >= Upper Band
        Short TP: Price <= Lower Band
        """
        if position.side == OrderSide.BUY:
            tp_hit = current_price >= bb.upper
            if tp_hit:
                logger.info(f"TAKE PROFIT (LONG): Current {current_price} >= BB Upper {bb.upper}")
            return tp_hit
        else:
            tp_hit = current_price <= bb.lower
            if tp_hit:
                logger.info(f"TAKE PROFIT (SHORT): Current {current_price} <= BB Lower {bb.lower}")
            return tp_hit

    def calculate_initial_stop(self, position: Position) -> float:
        """
        Calculate the initial trailing stop price.
        
        Long: entry_price * (1 - trail_pct)
        Short: entry_price * (1 + trail_pct)
        """
        trail_pct = self.cfg.trail_pct
        
        if position.side == OrderSide.BUY:
            return position.entry_price * (1 - trail_pct)
        else:
            return position.entry_price * (1 + trail_pct)

    def update_trailing_stop(self, position: Position, current_price: float) -> float:
        """
        Update the trailing stop based on highest/lowest price since entry.
        
        Long: Tracks highest price. Stop = highest_price * (1 - trail_pct)
              Only moves UP (never down).
        Short: Tracks lowest price. Stop = lowest_price * (1 + trail_pct)
               Only moves DOWN (never up).
        
        Returns:
            New trailing stop price (or current one if no change needed)
        """
        trail_pct = self.cfg.trail_pct

        if position.side == OrderSide.BUY:
            # Track highest price since entry
            if current_price > position.highest_price:
                position.highest_price = current_price
                new_stop = position.highest_price * (1 - trail_pct)
                # Only move UP
                if new_stop > position.trailing_stop_price:
                    position.trailing_stop_price = new_stop
                    logger.info(
                        f"TRAILING STOP UPDATED (LONG): "
                        f"New High={position.highest_price:.2f}, "
                        f"Stop={new_stop:.2f}"
                    )
        
        else:  # SHORT
            # Track lowest price since entry
            if current_price < position.lowest_price:
                position.lowest_price = current_price
                new_stop = position.lowest_price * (1 + trail_pct)
                # Only move DOWN
                if new_stop < position.trailing_stop_price:
                    position.trailing_stop_price = new_stop
                    logger.info(
                        f"TRAILING STOP UPDATED (SHORT): "
                        f"New Low={position.lowest_price:.2f}, "
                        f"Stop={new_stop:.2f}"
                    )

        return position.trailing_stop_price

    def check_stop_loss(self, position: Position, current_price: float) -> bool:
        """
        Check if the trailing stop has been hit.
        
        Long: current_price <= trailing_stop_price
        Short: current_price >= trailing_stop_price
        """
        if position.side == OrderSide.BUY:
            stop_hit = current_price <= position.trailing_stop_price
            if stop_hit:
                logger.info(
                    f"STOP LOSS HIT (LONG): Current {current_price:.2f} <= "
                    f"Stop {position.trailing_stop_price:.2f}"
                )
            return stop_hit
        else:
            stop_hit = current_price >= position.trailing_stop_price
            if stop_hit:
                logger.info(
                    f"STOP LOSS HIT (SHORT): Current {current_price:.2f} >= "
                    f"Stop {position.trailing_stop_price:.2f}"
                )
            return stop_hit

    def get_ohlcv_for_display(self) -> pd.DataFrame:
        """Get last N candles for display purposes."""
        if self._df is None:
            return pd.DataFrame()
        display_window = min(self.cfg.display_window, len(self._df))
        return self._df.tail(display_window)