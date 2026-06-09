"""
risk_manager.py - Daily Risk Limit Manager

Enforces daily loss limits and trade count limits.
All counters reset at midnight IST.
"""

import logging
import time
from datetime import datetime, timedelta
from typing import List, Optional, Tuple
from dataclasses import dataclass, field

import pytz

from config import RiskConfig

logger = logging.getLogger("risk_manager")

IST = pytz.timezone("Asia/Kolkata")


@dataclass
class TradeRecord:
    """Record of a completed trade."""
    id: int
    side: str           # "LONG" or "SHORT"
    entry_time: float
    exit_time: float
    entry_price: float
    exit_price: float
    quantity: float
    pnl_usdt: float
    pnl_inr: float
    exit_reason: str    # "TP", "SL", "MANUAL"
    symbol: str = "BTC/USDT"


@dataclass
class DailyStats:
    """Daily trading statistics."""
    date: str
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    total_pnl_usdt: float = 0.0
    total_pnl_inr: float = 0.0
    max_drawdown_usdt: float = 0.0
    is_locked: bool = False  # Locked for the day


class RiskManager:
    """
    Manages risk limits including:
    - Max daily loss (in INR, converted to USDT)
    - Max trades per day
    - Daily reset at midnight IST
    """

    def __init__(self, cfg: RiskConfig):
        self.cfg = cfg
        self._current_date: str = ""  # "YYYY-MM-DD" in IST
        self._trades_today: int = 0
        self._daily_pnl_usdt: float = 0.0
        self._cumulative_pnl_usdt: float = 0.0  # Cumulative for drawdown tracking
        self._is_locked: bool = False
        self._lock_reason: str = ""
        self._trade_log: List[TradeRecord] = []
        self._trade_counter: int = 0
        self._stats_history: List[DailyStats] = []

    @property
    def ist_now(self) -> datetime:
        """Get current time in IST."""
        return datetime.now(IST)

    @property
    def today_str(self) -> str:
        """Get today's date string in IST (YYYY-MM-DD)."""
        return self.ist_now.strftime("%Y-%m-%d")

    @property
    def trades_today(self) -> int:
        return self._trades_today

    @property
    def daily_pnl_usdt(self) -> float:
        return self._daily_pnl_usdt

    @property
    def daily_pnl_inr(self) -> float:
        return self._daily_pnl_usdt * self.cfg.usd_inr_rate

    @property
    def is_locked(self) -> bool:
        return self._is_locked

    @property
    def lock_reason(self) -> str:
        return self._lock_reason

    @property
    def max_loss_usdt(self) -> float:
        return self.cfg.max_daily_loss_inr / self.cfg.usd_inr_rate

    def check_and_reset_daily(self):
        """Check if date has changed (midnight IST) and reset counters."""
        today = self.today_str
        
        if today != self._current_date:
            # Save previous day stats if any
            if self._current_date and self._trades_today > 0:
                prev_stats = DailyStats(
                    date=self._current_date,
                    total_trades=self._trades_today,
                    winning_trades=sum(1 for t in self._trade_log[-self._trades_today:] 
                                      if t.pnl_usdt > 0),
                    losing_trades=sum(1 for t in self._trade_log[-self._trades_today:] 
                                     if t.pnl_usdt <= 0),
                    total_pnl_usdt=self._daily_pnl_usdt,
                    total_pnl_inr=self.daily_pnl_inr,
                    is_locked=self._is_locked,
                )
                self._stats_history.append(prev_stats)

            # Reset for new day
            logger.info(f"🔄 NEW DAY: Resetting daily counters for {today}")
            self._current_date = today
            self._trades_today = 0
            self._daily_pnl_usdt = 0.0
            self._cumulative_pnl_usdt = 0.0
            self._is_locked = False
            self._lock_reason = ""

    def record_trade(self, side: str, entry_price: float, exit_price: float,
                     quantity: float, exit_reason: str, entry_time: float,
                     exit_time: float, symbol: str = "BTC/USDT") -> TradeRecord:
        """
        Record a completed trade and update daily P&L.
        
        For LONG: P&L = (exit_price - entry_price) * quantity
        For SHORT: P&L = (entry_price - exit_price) * quantity
        """
        if side.upper() in ("LONG", "BUY"):
            pnl_usdt = (exit_price - entry_price) * quantity
        else:
            pnl_usdt = (entry_price - exit_price) * quantity

        self._trade_counter += 1
        self._trades_today += 1
        self._daily_pnl_usdt += pnl_usdt
        self._cumulative_pnl_usdt += pnl_usdt

        record = TradeRecord(
            id=self._trade_counter,
            side=side.upper(),
            entry_time=entry_time,
            exit_time=exit_time,
            entry_price=entry_price,
            exit_price=exit_price,
            quantity=quantity,
            pnl_usdt=pnl_usdt,
            pnl_inr=pnl_usdt * self.cfg.usd_inr_rate,
            exit_reason=exit_reason,
            symbol=symbol,
        )
        self._trade_log.append(record)

        logger.info(
            f"📝 TRADE #{record.id} RECORDED: {record.side} | "
            f"Entry: {entry_price:.2f} | Exit: {exit_price:.2f} | "
            f"Qty: {quantity:.6f} | PnL: ₹{record.pnl_inr:.2f} | "
            f"Reason: {exit_reason} | "
            f"Daily PnL: ₹{self.daily_pnl_inr:.2f}"
        )

        return record

    def check_limits(self) -> Tuple[bool, str]:
        """
        Check if any daily limits have been breached.
        
        Returns:
            (is_ok: bool, reason: str) - True if trading can continue
        """
        self.check_and_reset_daily()

        # Check max trades
        if self._trades_today >= self.cfg.max_trades_per_day:
            self._is_locked = True
            self._lock_reason = f"Max trades ({self.cfg.max_trades_per_day}) reached for today"
            logger.warning(f"🔒 {self._lock_reason}")
            return False, self._lock_reason

        # Check max daily loss (in USDT)
        max_loss = self.max_loss_usdt
        if self._daily_pnl_usdt <= -max_loss:
            self._is_locked = True
            self._lock_reason = (
                f"Max daily loss ₹{self.cfg.max_daily_loss_inr:.2f} "
                f"(${max_loss:.2f}) breached! Daily PnL: ₹{self.daily_pnl_inr:.2f}"
            )
            logger.warning(f"🔒 {self._lock_reason}")
            return False, self._lock_reason

        # If already locked from a previous check
        if self._is_locked:
            return False, self._lock_reason

        return True, "OK"

    def can_enter_new_trade(self) -> bool:
        """Check if a new trade can be entered based on daily limits."""
        ok, _ = self.check_limits()
        return ok

    def get_recent_trades(self, n: int = 20) -> List[TradeRecord]:
        """Get the most recent N trades."""
        return self._trade_log[-n:]

    def get_stats_summary(self) -> str:
        """Get a summary string of current risk stats."""
        return (
            f"Trades Today: {self._trades_today}/{self.cfg.max_trades_per_day} | "
            f"Daily PnL: ₹{self.daily_pnl_inr:.2f} (${self._daily_pnl_usdt:.2f}) | "
            f"Max Loss Limit: ₹{self.cfg.max_daily_loss_inr:.2f} (${self.max_loss_usdt:.2f}) | "
            f"Status: {'🔒 LOCKED' if self._is_locked else '✅ ACTIVE'}"
            + (f" ({self._lock_reason})" if self._is_locked else "")
        )

    def print_recent_trades(self, n: int = 20):
        """Print recent trades to console."""
        recent = self.get_recent_trades(n)
        if not recent:
            print("  No trades executed yet.")
            return

        print(f"\n  {'ID':<5} {'Side':<8} {'Entry':<12} {'Exit':<12} {'PnL (₹)':<12} {'Reason':<8}")
        print(f"  {'-'*5} {'-'*8} {'-'*12} {'-'*12} {'-'*12} {'-'*8}")
        for t in reversed(recent):
            pnl_str = f"₹{t.pnl_inr:+.2f}"
            pnl_color = "+" if t.pnl_usdt > 0 else "-"
            print(
                f"  {t.id:<5} {t.side:<8} {t.entry_price:<12.2f} "
                f"{t.exit_price:<12.2f} {pnl_str:<12} {t.exit_reason:<8}"
            )