"""
cli_display.py - Live CLI Display Module

Renders a rich live-updating CLI dashboard showing:
- BTC live price
- Account balance (USDT, BTC)
- P&L (daily, unrealized)
- Current position details
- Trailing stop price
- Recent trade log
- Strategy config
- Session status

Uses the `rich` library for terminal UI.
"""

import time
import logging
from datetime import datetime
from typing import Optional, List, Dict, Any

import pytz
from rich.console import Console
from rich.live import Live
from rich.table import Table
from rich.panel import Panel
from rich.layout import Layout
from rich.text import Text
from rich.align import Align
from rich.columns import Columns
from rich import box

from config import BotConfig
from exchange_client import Position, OrderSide
from strategy import BBResult, StrategyConfig
from risk_manager import RiskManager, TradeRecord

logger = logging.getLogger("cli_display")

IST = pytz.timezone("Asia/Kolkata")

# ─── Color constants ───
GREEN = "green"
RED = "red"
YELLOW = "yellow"
CYAN = "cyan"
WHITE = "white"
MAGENTA = "magenta"
BRIGHT_GREEN = "bright_green"
BRIGHT_RED = "bright_red"
GOLD = "gold1"
DIM = "dim"


class CLIDisplay:
    """
    Rich-based live CLI dashboard for the trading bot.
    Renders continuously updating panels with trading data.
    """

    def __init__(self, cfg: BotConfig):
        self.cfg = cfg
        self.console = Console()
        self._live: Optional[Live] = None
        self._start_time = time.time()

        # Data holders (updated by bot each cycle)
        self.current_price: float = 0.0
        self.prev_price: float = 0.0
        self.balance: Dict[str, Any] = {}
        self.position: Optional[Position] = None
        self.bb_result: Optional[BBResult] = None
        self.signal: str = "NONE"
        self.signal_reason: str = ""
        self.last_signal_distance: float = 0.0
        self.in_session: bool = False
        self.session_info: str = ""
        self.bot_status: str = "IDLE"
        self.cycle_count: int = 0
        self.last_error: str = ""
        self.paper_trading: bool = False
        self.risk_manager: Optional[RiskManager] = None
        self.recent_trades: List[TradeRecord] = []

    def update_data(self, **kwargs):
        """Update display data from the bot loop."""
        for key, value in kwargs.items():
            if hasattr(self, key):
                # Track previous price for direction arrow
                if key == "current_price" and self.current_price != 0:
                    self.prev_price = self.current_price
                setattr(self, key, value)

    def start_live_display(self):
        """Start the live updating display."""
        self._live = Live(
            self._generate_layout(),
            console=self.console,
            refresh_per_second=1,
            screen=True,
        )
        self._live.start()

    def stop_live_display(self):
        """Stop the live display."""
        if self._live:
            self._live.stop()

    def refresh(self):
        """Refresh the live display with latest data."""
        if self._live:
            self._live.update(self._generate_layout())

    def _generate_layout(self) -> Layout:
        """Generate the full terminal layout."""
        layout = Layout()
        layout.split_column(
            Layout(name="header", size=3),
            Layout(name="body"),
            Layout(name="footer", size=3),
        )
        layout["body"].split_row(
            Layout(name="left", ratio=2),
            Layout(name="right", ratio=3),
        )
        layout["left"].split_column(
            Layout(name="price_balance"),
            Layout(name="position"),
            Layout(name="strategy_config"),
        )
        layout["right"].split_column(
            Layout(name="pnl_signal"),
            Layout(name="trades"),
        )

        layout["header"].update(self._header_panel())
        layout["price_balance"].update(self._price_balance_panel())
        layout["position"].update(self._position_panel())
        layout["strategy_config"].update(self._strategy_config_panel())
        layout["pnl_signal"].update(self._pnl_signal_panel())
        layout["trades"].update(self._trades_panel())
        layout["footer"].update(self._footer_panel())

        return layout

    # ─── PANEL BUILDERS ───

    def _header_panel(self) -> Panel:
        """Header with bot name, status, and time."""
        now = datetime.now(IST)
        runtime = time.time() - self._start_time
        hours = int(runtime // 3600)
        mins = int((runtime % 3600) // 60)
        secs = int(runtime % 60)

        status_color = "green" if self.bot_status == "RUNNING" else (
            "red" if "ERROR" in self.bot_status else "yellow"
        )
        session_color = "green" if self.in_session else "dim"

        text = Text()
        text.append(" 🤖 ", style="bold")
        text.append("BOLLINGER BAND REVERSAL BOT", style="bold bright_cyan")
        text.append("  |  ", style="dim")
        text.append(f"Status: ", style="dim")
        text.append(f"{self.bot_status}", style=f"bold {status_color}")
        text.append("  |  ", style="dim")
        text.append(f"Session: ", style="dim")
        text.append(f"{'● TRADING' if self.in_session else '○ OUTSIDE'}", style=f"bold {session_color}")
        text.append("  |  ", style="dim")
        text.append(f"{now.strftime('%Y-%m-%d %H:%M:%S')} IST", style="bold white")
        text.append("  |  ", style="dim")
        text.append(f"Uptime: {hours:02d}:{mins:02d}:{secs:02d}", style="cyan")

        if self.paper_trading:
            text.append("  |  ", style="dim")
            text.append("📝 PAPER", style="bold yellow")

        return Panel(text, box=box.HEAVY, border_style="cyan")

    def _price_balance_panel(self) -> Panel:
        """Live BTC price and account balances."""
        table = Table(box=None, show_header=False, padding=(0, 1))
        table.add_column("Label", style="dim", width=15)
        table.add_column("Value", style="bold white")

        # Price with direction indicator
        price_str = f"${self.current_price:,.2f}" if self.current_price else "---"
        if self.prev_price and self.current_price:
            if self.current_price > self.prev_price:
                price_str += " ▲"
                price_color = "bold bright_green"
            elif self.current_price < self.prev_price:
                price_str += " ▼"
                price_color = "bold bright_red"
            else:
                price_color = "bold white"
        else:
            price_color = "bold white"

        table.add_row("BTC/USDT", Text(price_str, style=price_color))

        # Balances
        if self.balance:
            usdt = self.balance.get("USDT", {})
            btc = self.balance.get("BTC", {})
            
            usdt_free = usdt.get("free", 0)
            usdt_total = usdt.get("total", 0)
            btc_free = btc.get("free", 0)
            btc_total = btc.get("total", 0)

            table.add_row("", "")
            table.add_row("USDT Free", Text(f"${usdt_free:,.2f}", style="bright_green"))
            table.add_row("USDT Total", Text(f"${usdt_total:,.2f}", style="green"))
            table.add_row("", "")
            table.add_row("BTC Free", Text(f"{btc_free:,.6f}", style="bright_cyan"))
            table.add_row("BTC Total", Text(f"{btc_total:,.6f}", style="cyan"))

            # INR equivalent
            if self.current_price and btc_total:
                inr_val = btc_total * self.current_price * self.cfg.risk.usd_inr_rate
                table.add_row("BTC in ₹", Text(f"₹{inr_val:,.2f}", style="gold1"))

        usdt_total_val = self.balance.get("USDT", {}).get("total", 0) if self.balance else 0
        if self.current_price:
            inr_equiv = usdt_total_val * self.cfg.risk.usd_inr_rate
            table.add_row("Total in ₹", Text(f"₹{inr_equiv:,.2f}", style="bold gold1"))

        return Panel(table, title="💰 Live Price & Balance", border_style="blue",
                     title_align="left")

    def _position_panel(self) -> Panel:
        """Current position details."""
        if not self.position:
            text = Text("\n  No active position\n", style="dim")
            return Panel(text, title="📊 Current Position", border_style="blue",
                        title_align="left")

        pos = self.position
        side_color = "bright_green" if pos.side == OrderSide.BUY else "bright_red"
        side_emoji = "🟢 LONG" if pos.side == OrderSide.BUY else "🔴 SHORT"

        table = Table(box=None, show_header=False, padding=(0, 1))
        table.add_column("Label", style="dim", width=18)
        table.add_column("Value", style="bold white")

        table.add_row("Direction", Text(side_emoji, style=f"bold {side_color}"))
        table.add_row("Entry Price", Text(f"${pos.entry_price:,.2f}", style="white"))
        table.add_row("Quantity (BTC)", Text(f"{pos.quantity:,.6f}", style="cyan"))
        table.add_row("Invested (USDT)", Text(f"${pos.usdt_invested:,.2f}", style="white"))
        table.add_row("Invested (INR)", Text(f"₹{pos.inr_invested:,.2f}", style="gold1"))

        # Unrealized P&L
        if self.current_price:
            if pos.side == OrderSide.BUY:
                unreal_pnl = (self.current_price - pos.entry_price) * pos.quantity
            else:
                unreal_pnl = (pos.entry_price - self.current_price) * pos.quantity
            unreal_pnl_inr = unreal_pnl * self.cfg.risk.usd_inr_rate
            pnl_pct = (unreal_pnl / pos.usdt_invested * 100) if pos.usdt_invested else 0

            pnl_color = "bright_green" if unreal_pnl >= 0 else "bright_red"
            pnl_sign = "+" if unreal_pnl >= 0 else ""
            table.add_row("Unrealized P&L (USDT)", 
                         Text(f"{pnl_sign}${unreal_pnl:,.2f} ({pnl_sign}{pnl_pct:.2f}%)",
                              style=f"bold {pnl_color}"))
            table.add_row("Unrealized P&L (INR)",
                         Text(f"{pnl_sign}₹{unreal_pnl_inr:,.2f}", style=f"bold {pnl_color}"))

        # Trailing stop
        table.add_row("", "")
        table.add_row("Trailing Stop",
                     Text(f"${pos.trailing_stop_price:,.2f}", style="bold yellow"))
        if pos.side == OrderSide.BUY:
            table.add_row("Highest Since Entry",
                         Text(f"${pos.highest_price:,.2f}", style="bright_green"))
        else:
            table.add_row("Lowest Since Entry",
                         Text(f"${pos.lowest_price:,.2f}", style="bright_green"))

        # Entry time
        entry_dt = datetime.fromtimestamp(pos.entry_time, tz=IST)
        table.add_row("Entry Time",
                     Text(entry_dt.strftime("%Y-%m-%d %H:%M:%S IST"), style="white"))

        return Panel(table, title="📊 Current Position", border_style="blue",
                    title_align="left")

    def _strategy_config_panel(self) -> Panel:
        """Show current strategy configuration."""
        st = self.cfg.strategy
        rk = self.cfg.risk

        lines = []
        lines.append(f"  BB Period:        {st.bb_period}")
        lines.append(f"  BB Std Dev:       {st.bb_std_dev}")
        lines.append(f"  Near Threshold:   {st.near_threshold*100:.1f}%")
        lines.append(f"  Trail Stop:       {st.trail_pct*100:.1f}%")
        lines.append(f"  Trade Size:       ₹{rk.trade_size_inr:,.0f}")
        lines.append(f"  USD/INR Rate:     ₹{rk.usd_inr_rate}")
        lines.append(f"  Max Daily Loss:   ₹{rk.max_daily_loss_inr:,.0f}")
        lines.append(f"  Max Trades/Day:   {rk.max_trades_per_day}")
        lines.append(f"  Poll Interval:    {self.cfg.poll_interval_seconds}s")
        lines.append(f"  Exchange:         {self.cfg.exchange.exchange_name}")
        lines.append(f"  Long Only:        {'Yes' if self.cfg.exchange.long_only else 'No'}")

        text = Text("\n".join(lines), style="cyan")
        return Panel(text, title="⚙️ Strategy Config", border_style="magenta",
                    title_align="left")

    def _pnl_signal_panel(self) -> Panel:
        """P&L summary and latest signal info."""
        layout = Layout()
        layout.split_row(
            Layout(name="pnl"),
            Layout(name="signal"),
        )

        # P&L sub-panel
        pnl_table = Table(box=None, show_header=False, padding=(0, 1))
        pnl_table.add_column("Label", style="dim", width=16)
        pnl_table.add_column("Value", style="bold")

        if self.risk_manager:
            pnl_table.add_row("Daily P&L (USDT)",
                            Text(f"${self.risk_manager.daily_pnl_usdt:+,.2f}",
                                 style="bold bright_green" if self.risk_manager.daily_pnl_usdt >= 0 else "bold bright_red"))
            pnl_table.add_row("Daily P&L (INR)",
                            Text(f"₹{self.risk_manager.daily_pnl_inr:+,.2f}",
                                 style="bold bright_green" if self.risk_manager.daily_pnl_inr >= 0 else "bold bright_red"))
            pnl_table.add_row("Trades Today",
                            Text(f"{self.risk_manager.trades_today}/{self.cfg.risk.max_trades_per_day}",
                                 style="bold white"))
            pnl_table.add_row("Max Loss Limit",
                            Text(f"₹{self.cfg.risk.max_daily_loss_inr:,.0f}", style="yellow"))
            
            if self.risk_manager.is_locked:
                pnl_table.add_row("", "")
                pnl_table.add_row("STATUS", Text("🔒 LOCKED", style="bold bright_red"))
                pnl_table.add_row("Reason", Text(self.risk_manager.lock_reason, style="red"))
            else:
                pnl_table.add_row("STATUS", Text("✅ ACTIVE", style="bold bright_green"))

        layout["pnl"].update(Panel(pnl_table, title="📈 P&L Summary", border_style="green",
                                   title_align="left"))

        # Signal sub-panel
        signal_table = Table(box=None, show_header=False, padding=(0, 1))
        signal_table.add_column("Label", style="dim", width=16)
        signal_table.add_column("Value", style="bold")

        signal_color = {
            "LONG": "bold bright_green",
            "SHORT": "bold bright_red",
            "NONE": "dim white",
        }.get(self.signal, "white")
        signal_emoji = {
            "LONG": "🟢",
            "SHORT": "🔴",
            "NONE": "⚪",
        }.get(self.signal, "⚪")

        signal_table.add_row("Last Signal",
                           Text(f"{signal_emoji} {self.signal}", style=signal_color))
        if self.last_signal_distance:
            signal_table.add_row("Band Distance",
                               Text(f"{self.last_signal_distance:.3f}%", style="cyan"))
        if self.signal_reason:
            signal_table.add_row("Reason",
                               Text(self.signal_reason, style="dim"))

        if self.bb_result and self.bb_result.sma > 0:
            signal_table.add_row("", "")
            signal_table.add_row("BB Upper",
                               Text(f"${self.bb_result.upper:,.2f}", style="bright_red"))
            signal_table.add_row("BB SMA",
                               Text(f"${self.bb_result.sma:,.2f}", style="white"))
            signal_table.add_row("BB Lower",
                               Text(f"${self.bb_result.lower:,.2f}", style="bright_green"))
            signal_table.add_row("BB Width",
                               Text(f"${self.bb_result.width:,.2f}", style="dim"))
            signal_table.add_row("Volatility σ",
                               Text(f"${self.bb_result.volatility:,.2f}", style="dim"))

        layout["signal"].update(Panel(signal_table, title="🎯 Signal & BB", border_style="yellow",
                                      title_align="left"))

        return Panel(layout, border_style="")

    def _trades_panel(self) -> Panel:
        """Recent trade log."""
        table = Table(
            box=box.SIMPLE,
            show_header=True,
            header_style="bold cyan",
            padding=(0, 1),
        )
        table.add_column("#", style="dim", width=4)
        table.add_column("Side", width=6)
        table.add_column("Entry", width=10)
        table.add_column("Exit", width=10)
        table.add_column("Qty", width=8)
        table.add_column("PnL (₹)", width=12)
        table.add_column("Reason", width=8)

        if not self.recent_trades:
            table.add_row("", "", "", "", "", "No trades yet", "")
        else:
            for t in self.recent_trades[-10:][::-1]:  # Last 10, newest first
                side_color = "bright_green" if t.side in ("LONG", "BUY") else "bright_red"
                pnl_color = "bright_green" if t.pnl_usdt > 0 else "bright_red"
                pnl_sign = "+" if t.pnl_usdt >= 0 else ""
                
                table.add_row(
                    str(t.id),
                    Text(t.side, style=side_color),
                    f"${t.entry_price:,.2f}",
                    f"${t.exit_price:,.2f}",
                    f"{t.quantity:,.6f}",
                    Text(f"{pnl_sign}₹{t.pnl_inr:,.2f}", style=pnl_color),
                    t.exit_reason,
                )

        return Panel(table, title="📋 Recent Trade Log", border_style="yellow",
                    title_align="left")

    def _footer_panel(self) -> Panel:
        """Footer with cycle info, errors, and session times."""
        text = Text()
        text.append(f"Cycle: #{self.cycle_count}  |  ", style="dim")
        text.append(f"Interval: {self.cfg.poll_interval_seconds}s  |  ", style="dim")
        
        ses = self.cfg.session
        text.append(
            f"Sessions: {ses.morning_start}-{ses.morning_end}, "
            f"{ses.afternoon_start}-{ses.afternoon_end}, "
            f"{ses.evening_start}-{ses.evening_end} IST",
            style="dim"
        )

        if self.last_error:
            text.append(f"\n⚠️  Last Error: {self.last_error}", style="bold red")

        return Panel(text, box=box.SIMPLE, border_style="dim")

    # ─── STATIC PRINT METHODS (for non-live mode / logging) ───

    def print_status_line(self, msg: str, style: str = "white"):
        """Print a single status line."""
        timestamp = datetime.now(IST).strftime("%H:%M:%S")
        self.console.print(f"[dim]{timestamp}[/dim] {msg}", style=style)

    def print_trade_executed(self, side: str, entry: float, qty: float, 
                             usdt_val: float, inr_val: float):
        """Print trade execution details."""
        side_color = "bright_green" if side in ("BUY", "LONG") else "bright_red"
        side_emoji = "🟢" if side in ("BUY", "LONG") else "🔴"
        self.print_status_line(
            f"{side_emoji} [{side_color}]TRADE EXECUTED[/{side_color}] | "
            f"{side} @ ${entry:,.2f} | Qty: {qty:,.6f} BTC | "
            f"Value: ${usdt_val:,.2f} (₹{inr_val:,.2f})",
            style="bold white"
        )

    def print_trade_exit(self, side: str, exit_price: float, pnl_usdt: float, 
                         pnl_inr: float, reason: str):
        """Print trade exit details."""
        pnl_color = "bright_green" if pnl_usdt >= 0 else "bright_red"
        pnl_sign = "+" if pnl_usdt >= 0 else ""
        self.print_status_line(
            f"❌ [bold]POSITION CLOSED[/bold] | {reason} | "
            f"Exit: ${exit_price:,.2f} | "
            f"P&L: {pnl_sign}${pnl_usdt:,.2f} ({pnl_sign}₹{pnl_inr:,.2f})",
            style=pnl_color
        )

    def print_signal_detected(self, signal: str, reason: str):
        """Print detected signal."""
        if signal == "NONE":
            return
        color = "bright_green" if signal == "LONG" else "bright_red"
        emoji = "🟢" if signal == "LONG" else "🔴"
        self.print_status_line(
            f"{emoji} [bold {color}]SIGNAL: {signal}[/bold {color}] | {reason}",
            style="bold"
        )

    def print_error(self, error_msg: str):
        """Print error message."""
        self.print_status_line(f"⚠️ [bold red]ERROR:[/bold red] {error_msg}", style="red")