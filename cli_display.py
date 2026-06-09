"""
cli_display.py - Interactive Live CLI Dashboard

Renders a rich live-updating CLI dashboard with interactive controls.
Users can modify settings on-the-fly using keyboard hotkeys.

Hotkeys (anytime):
  [M]  - Open settings menu
  [P]  - Pause / Resume bot trading
  [H]  - Show help overlay (3s)

Menu hotkeys (when settings menu is open):
  [1-9] - Select a setting to edit
  [Enter] - Confirm new value
  [Backspace] - Delete last character / go back
  [Esc/Q] - Close menu and return to dashboard
  [R]     - Reset daily stats
"""

import os
import sys
import time
import queue
import threading
import logging
from datetime import datetime
from typing import Optional, List, Dict, Any, Tuple

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


# =============================================================================
# Keyboard Listener (cross-platform non-blocking stdin)
# =============================================================================

class KeyboardListener:
    """
    Background thread that reads stdin without blocking.
    Works on Windows (msvcrt) and Unix (select + termios).
    Silently disables itself if stdin is not a TTY (daemon mode).
    """

    def __init__(self):
        self._queue: queue.Queue = queue.Queue()
        self._running: bool = False
        self._thread: Optional[threading.Thread] = None
        self._old_termios: Any = None
        self._fd: Optional[int] = None

    @property
    def pending(self) -> bool:
        """Check if there are unread keystrokes."""
        return not self._queue.empty()

    def get_key(self) -> Optional[str]:
        """Get the next keystroke, or None if queue is empty."""
        try:
            return self._queue.get_nowait()
        except queue.Empty:
            return None

    def drain(self) -> List[str]:
        """Drain all pending keystrokes from the queue."""
        keys = []
        while not self._queue.empty():
            try:
                keys.append(self._queue.get_nowait())
            except queue.Empty:
                break
        return keys

    def start(self):
        """Start the background keyboard listener."""
        if not sys.stdin.isatty():
            logger.debug("stdin is not a TTY; keyboard listener disabled")
            return

        self._running = True
        self._thread = threading.Thread(target=self._listen, daemon=True)
        self._thread.start()
        logger.debug("Keyboard listener started")

    def stop(self):
        """Stop the listener and restore terminal settings."""
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=0.5)
        self._restore_terminal()
        logger.debug("Keyboard listener stopped")

    def _listen(self):
        """Main listen loop - dispatches to platform-specific handler."""
        try:
            if os.name == 'nt':
                self._listen_windows()
            else:
                self._listen_unix()
        except Exception as e:
            logger.debug(f"Keyboard listener error: {e}")

    def _listen_windows(self):
        """Windows: use msvcrt for non-blocking console input."""
        import msvcrt
        while self._running:
            try:
                if msvcrt.kbhit():
                    ch = msvcrt.getch()
                    try:
                        key = ch.decode('utf-8', errors='ignore').lower()
                    except (UnicodeDecodeError, AttributeError):
                        # Special keys (arrows, etc.) - encode as readable tag
                        if ch == b'\xe0':  # Extended key prefix
                            ch2 = msvcrt.getch()
                            key = f"<special:{ch2[0]}>"
                        else:
                            key = f"<char:{ch[0]}>"
                    self._queue.put(key)
                else:
                    time.sleep(0.05)
            except Exception:
                time.sleep(0.1)

    def _listen_unix(self):
        """Unix/Linux: use select + termios for non-blocking input."""
        import select
        import termios
        import tty

        self._fd = sys.stdin.fileno()
        try:
            self._old_termios = termios.tcgetattr(self._fd)
        except (termios.error, IOError):
            logger.debug("Cannot get terminal attributes; listener disabled")
            return

        try:
            tty.setraw(self._fd)
            while self._running:
                try:
                    r, _, _ = select.select([sys.stdin], [], [], 0.1)
                    if r:
                        ch = sys.stdin.read(1)
                        if ch:
                            # Translate common control characters
                            if ch == '\x03':       # Ctrl+C
                                self._queue.put('<ctrl-c>')
                            elif ch == '\x1b':     # Escape
                                self._queue.put('<esc>')
                            elif ch == '\x7f':     # Backspace (DEL)
                                self._queue.put('<backspace>')
                            elif ch == '\r' or ch == '\n':
                                self._queue.put('<enter>')
                            elif ch == '\t':
                                self._queue.put('<tab>')
                            else:
                                self._queue.put(ch.lower())
                except (select.error, IOError, OSError):
                    time.sleep(0.1)
                except Exception:
                    time.sleep(0.1)
        finally:
            self._restore_terminal()

    def _restore_terminal(self):
        """Restore terminal to original settings."""
        if self._old_termios is not None and self._fd is not None:
            import termios
            try:
                termios.tcsetattr(self._fd, termios.TCSADRAIN, self._old_termios)
                self._old_termios = None
            except (termios.error, IOError, OSError):
                pass


# =============================================================================
# Settings Menu Definition
# =============================================================================

# Each entry: (key, label, config_path, current_getter, formatter, parser)
# config_path: dotted path within BotConfig, e.g. "strategy.bb_period"
MenuEntry = Tuple[str, str, str, callable, callable, callable]

MENU_ENTRIES: List[MenuEntry] = [
    # Strategy
    ("1", "BB Period",           "strategy.bb_period",
     lambda cfg: str(cfg.strategy.bb_period),
     lambda cfg: f"{cfg.strategy.bb_period}",
     lambda cfg, v: setattr(cfg.strategy, 'bb_period', int(v))),
    ("2", "BB Std Dev",          "strategy.bb_stddev",
      lambda cfg: str(cfg.strategy.bb_stddev),
      lambda cfg: f"{cfg.strategy.bb_stddev:.1f}",
      lambda cfg, v: setattr(cfg.strategy, 'bb_stddev', float(v))),
    ("3", "Near Threshold %",    "strategy.near_threshold",
     lambda cfg: str(cfg.strategy.near_threshold * 100),
     lambda cfg: f"{cfg.strategy.near_threshold * 100:.1f}%",
     lambda cfg, v: setattr(cfg.strategy, 'near_threshold', float(v) / 100.0)),
    ("4", "Trail Stop %",        "strategy.trail_pct",
     lambda cfg: str(cfg.strategy.trail_pct * 100),
     lambda cfg: f"{cfg.strategy.trail_pct * 100:.1f}%",
     lambda cfg, v: setattr(cfg.strategy, 'trail_pct', float(v) / 100.0)),
    # Risk
    ("5", "Trade Size (₹)",      "risk.trade_size_inr",
     lambda cfg: str(cfg.risk.trade_size_inr),
     lambda cfg: f"₹{cfg.risk.trade_size_inr:,.0f}",
     lambda cfg, v: setattr(cfg.risk, 'trade_size_inr', float(v))),
    ("6", "USD/INR Rate",        "risk.usd_inr_rate",
     lambda cfg: str(cfg.risk.usd_inr_rate),
     lambda cfg: f"₹{cfg.risk.usd_inr_rate}",
     lambda cfg, v: setattr(cfg.risk, 'usd_inr_rate', float(v))),
    ("7", "Max Daily Loss (₹)",  "risk.max_daily_loss_inr",
     lambda cfg: str(cfg.risk.max_daily_loss_inr),
     lambda cfg: f"₹{cfg.risk.max_daily_loss_inr:,.0f}",
     lambda cfg, v: setattr(cfg.risk, 'max_daily_loss_inr', float(v))),
    ("8", "Max Trades/Day",      "risk.max_trades_per_day",
     lambda cfg: str(cfg.risk.max_trades_per_day),
     lambda cfg: f"{cfg.risk.max_trades_per_day}",
     lambda cfg, v: setattr(cfg.risk, 'max_trades_per_day', int(v))),
    # Other
    ("9", "Poll Interval (s)",   "poll_interval_sec",
      lambda cfg: str(cfg.poll_interval_sec),
      lambda cfg: f"{cfg.poll_interval_sec}s",
      lambda cfg, v: setattr(cfg, 'poll_interval_sec', int(v))),
]


# =============================================================================
# CLIDisplay Class
# =============================================================================

class CLIDisplay:
    """
    Rich-based interactive live CLI dashboard for the trading bot.

    Features:
    - Live updating price, balance, position, P&L panels
    - Keyboard hotkeys for on-the-fly settings changes
    - Interactive settings menu rendered within the live display
    """

    def __init__(self, cfg: BotConfig):
        self.cfg = cfg
        self.console = Console()
        self._live: Optional[Live] = None
        self._start_time = time.time()

        # ── Keyboard / interaction ──
        self._keyboard = KeyboardListener()
        self._action_queue: queue.Queue = queue.Queue()  # Actions for the bot
        self._menu_mode: bool = False
        self._menu_state: str = "main"       # "main" | "input"
        self._menu_input_buffer: str = ""
        self._menu_selected_key: str = ""
        self._menu_message: str = ""         # Success/error message
        self._menu_msg_time: float = 0.0     # When message was set
        self._help_overlay_until: float = 0.0  # Timestamp for help overlay

        # ── Data holders (updated by bot each cycle) ──
        self.current_price: float = 0.0
        self.prev_price: float = 0.0
        self.balance: Dict[str, Any] = {}
        self.position: Optional[Position] = None
        self.bb_result: Optional[BBResult] = None
        self.signal: str = "NONE"
        self.signal_reason: str = ""
        self.last_signal_distance: float = 0.0
        self.bot_status: str = "IDLE"
        self.cycle_count: int = 0
        self.last_error: str = ""
        self.paper_trading: bool = False
        self.paused: bool = False
        self.risk_manager: Optional[RiskManager] = None
        self.recent_trades: List[TradeRecord] = []

    # ─── Public API ──────────────────────────────────────────────────────

    def update_data(self, **kwargs):
        """Update display data from the bot loop."""
        for key, value in kwargs.items():
            if hasattr(self, key):
                # Track previous price for direction arrow
                if key == "current_price" and self.current_price != 0:
                    self.prev_price = self.current_price
                setattr(self, key, value)

    @property
    def pending_actions(self) -> List[Tuple[str, Any]]:
        """
        Drain and return all pending actions for the bot to process.

        Returns:
            List of (action_type, payload) tuples.
            Types: "pause", "resume", "update_config", "reset_daily"
        """
        actions = []
        while not self._action_queue.empty():
            try:
                actions.append(self._action_queue.get_nowait())
            except queue.Empty:
                break
        return actions

    # ─── Display Lifecycle ───────────────────────────────────────────────

    def start_live_display(self):
        """Start the live updating display and keyboard listener."""
        self._keyboard.start()
        self._live = Live(
            self._generate_layout(),
            console=self.console,
            refresh_per_second=4,       # Smooth but not excessive
            screen=False,               # False = no screen clear, critical for SSH
            auto_refresh=False,         # Manual refresh — only when data changes
            transient=False,
        )
        self._live.start()

    def stop_live_display(self):
        """Stop the live display and keyboard listener."""
        self._keyboard.stop()
        if self._live:
            self._live.stop()

    def refresh(self):
        """Refresh the live display and process keyboard input."""
        if self._live:
            # Process any pending keystrokes
            self._process_keystrokes()

            # Always update — Rich diffs internally to minimize redraw
            self._live.update(self._generate_layout(), refresh=True)

    def tick(self):
        """Lightweight tick: only process keystrokes, no render."""
        self._process_keystrokes()

    def shutdown(self):
        """Full cleanup - restore terminal, stop display."""
        self._keyboard.stop()
        if self._live:
            try:
                self._live.stop()
            except Exception:
                pass

    # ─── Keyboard / Interaction Handling ─────────────────────────────────

    def _process_keystrokes(self):
        """Process all pending keystrokes from the keyboard listener."""
        keys = self._keyboard.drain()
        if not keys:
            return

        for key in keys:
            self._handle_key(key)

    def _handle_key(self, key: str):
        """Handle a single keystroke."""

        # ── Global hotkeys (work even when menu is closed) ──
        if key == 'm' and not self._menu_mode:
            self._menu_mode = True
            self._menu_state = "main"
            self._menu_selected_key = ""
            self._menu_input_buffer = ""
            self._menu_message = ""
            return

        if key == 'p' and not self._menu_mode:
            # Toggle pause
            if self.paused:
                self._action_queue.put(("resume", None))
                self._menu_message = "✅ Bot RESUMED"
            else:
                self._action_queue.put(("pause", None))
                self._menu_message = "⏸️  Bot PAUSED"
            self._menu_msg_time = time.time()
            return

        if key == 'h' and not self._menu_mode:
            self._help_overlay_until = time.time() + 3.0
            return

        if key == '<ctrl-c>' and not self._menu_mode:
            self._action_queue.put(("shutdown", None))
            return

        # ── Menu keys (only when menu is open) ──
        if not self._menu_mode:
            return

        if key in ('<esc>', 'q'):
            # Close menu
            self._menu_mode = False
            self._menu_state = "main"
            self._menu_input_buffer = ""
            self._menu_selected_key = ""
            self._menu_message = ""
            return

        if key == 'r':
            # Reset daily stats (from menu)
            self._action_queue.put(("reset_daily", None))
            self._menu_message = "✅ Daily stats RESET"
            self._menu_msg_time = time.time()
            return

        if key == 'p' and self._menu_mode:
            # Pause from menu
            if self.paused:
                self._action_queue.put(("resume", None))
                self._menu_message = "✅ Bot RESUMED"
            else:
                self._action_queue.put(("pause", None))
                self._menu_message = "⏸️  Bot PAUSED"
            self._menu_msg_time = time.time()
            return

        # ── Input mode ──
        if self._menu_state == "input":
            if key == '<enter>':
                self._confirm_input()
                return
            elif key in ('<backspace>', '<esc>'):
                if self._menu_input_buffer:
                    self._menu_input_buffer = self._menu_input_buffer[:-1]
                else:
                    # Go back to main menu
                    self._menu_state = "main"
                    self._menu_selected_key = ""
                return
            elif len(key) == 1 and key.isprintable():
                self._menu_input_buffer += key
                return
            else:
                # Ignore other keys in input mode
                return

        # ── Main menu mode ──
        if self._menu_state == "main":
            # Check if it's a menu entry key (1-9)
            for entry_key, label, path, getter, fmt, parser in MENU_ENTRIES:
                if key == entry_key:
                    self._menu_state = "input"
                    self._menu_selected_key = entry_key
                    self._menu_input_buffer = getter(self.cfg)
                    return

    def _confirm_input(self):
        """Validate and apply the input buffer value to the config."""
        if not self._menu_selected_key:
            self._menu_state = "main"
            return

        # Find the matching menu entry
        entry = None
        for e in MENU_ENTRIES:
            if e[0] == self._menu_selected_key:
                entry = e
                break

        if entry is None:
            self._menu_state = "main"
            return

        _, label, config_path, getter, fmt, parser = entry

        try:
            parser(self.cfg, self._menu_input_buffer)
            self._action_queue.put(("update_config", {
                "path": config_path,
                "label": label,
                "value": self._menu_input_buffer,
            }))
            self._menu_message = f"✅ {label} updated!"
            self._menu_msg_time = time.time()
        except (ValueError, TypeError) as e:
            self._menu_message = f"❌ Invalid value: {e}"
            self._menu_msg_time = time.time()

        # Return to main menu
        self._menu_state = "main"
        self._menu_selected_key = ""
        self._menu_input_buffer = ""

    # ─── Layout Generation ───────────────────────────────────────────────

    def _generate_layout(self) -> Layout:
        """Generate the full terminal layout (dashboard or menu)."""
        if self._menu_mode:
            return self._generate_menu_layout()
        elif self._help_overlay_until > time.time():
            return self._generate_help_layout()
        else:
            return self._generate_dashboard_layout()

    def _generate_dashboard_layout(self) -> Layout:
        """Generate the standard trading dashboard layout."""
        layout = Layout()
        layout.split_column(
            Layout(name="header", size=3),
            Layout(name="body"),
            Layout(name="footer", size=4),
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

    # ─── Help Overlay ────────────────────────────────────────────────────

    def _generate_help_layout(self) -> Layout:
        """Show a help overlay with all available hotkeys."""
        layout = Layout()

        help_text = Text()
        help_text.append("\n")
        help_text.append("  🎮  COMMAND REFERENCE\n\n", style="bold bright_cyan")
        help_text.append("  [M]  Open Settings Menu  ", style="bold yellow")
        help_text.append("Edit strategy/risk params on-the-fly\n", style="dim")
        help_text.append("  [P]  Pause / Resume Bot  ", style="bold yellow")
        help_text.append("Halt or restart trading decisions\n", style="dim")
        help_text.append("  [H]  Show This Help      ", style="bold yellow")
        help_text.append("Display command reference (3s)\n", style="dim")
        help_text.append("  [Ctrl+C]  Shutdown Bot   ", style="bold yellow")
        help_text.append("Graceful shutdown\n", style="dim")
        help_text.append("\n  📋  MENU COMMANDS (when menu is open):\n\n", style="bold bright_cyan")
        help_text.append("  [1-9]  Select Setting    ", style="bold yellow")
        help_text.append("Edit a parameter\n", style="dim")
        help_text.append("  [Enter]  Confirm Input   ", style="bold yellow")
        help_text.append("Apply new value\n", style="dim")
        help_text.append("  [Esc/Q]  Close Menu      ", style="bold yellow")
        help_text.append("Return to dashboard\n", style="dim")
        help_text.append("  [R]  Reset Daily Stats   ", style="bold yellow")
        help_text.append("Clear today's counters\n", style="dim")

        panel = Panel(
            help_text,
            title="🆘 HELP",
            border_style="bright_cyan",
            title_align="center",
            padding=(1, 2),
        )
        layout.update(panel)
        return layout

    # ─── Menu Layout ─────────────────────────────────────────────────────

    def _generate_menu_layout(self) -> Layout:
        """Generate the interactive settings menu layout."""
        layout = Layout()
        layout.split_column(
            Layout(name="menu_header", size=3),
            Layout(name="menu_body"),
            Layout(name="menu_footer", size=4),
        )

        layout["menu_header"].update(self._menu_header_panel())
        layout["menu_body"].update(self._menu_body_panel())
        layout["menu_footer"].update(self._menu_footer_panel())

        return layout

    def _menu_header_panel(self) -> Panel:
        """Header for the settings menu."""
        text = Text()
        text.append(" ⚙️  ", style="bold")
        text.append("SETTINGS MENU", style="bold bright_cyan")

        if self._menu_message:
            # Show message for ~2 seconds
            if time.time() - self._menu_msg_time < 2.0:
                text.append("  —  ")
                text.append(self._menu_message, style="bold yellow")

        if self.paused:
            text.append("  |  ")
            text.append("⏸️ PAUSED", style="bold bright_red")

        return Panel(text, border_style="bright_cyan", title_align="left")

    def _menu_body_panel(self) -> Panel:
        """Body of the settings menu - shows all options."""
        if self._menu_state == "input":
            return self._menu_input_panel()

        # Main menu: show all settings
        lines = []
        lines.append("")
        lines.append("  📊  STRATEGY PARAMETERS")
        lines.append("  " + "─" * 50)

        for entry_key, label, path, getter, fmt, parser in MENU_ENTRIES[:4]:
            current = fmt(self.cfg)
            lines.append(f"    [{entry_key}]  {label:<22s} {current}")

        lines.append("")
        lines.append("  💰  RISK PARAMETERS")
        lines.append("  " + "─" * 50)

        for entry_key, label, path, getter, fmt, parser in MENU_ENTRIES[4:8]:
            current = fmt(self.cfg)
            lines.append(f"    [{entry_key}]  {label:<22s} {current}")

        lines.append("")
        lines.append("  ⏱️   OTHER")
        lines.append("  " + "─" * 50)

        for entry_key, label, path, getter, fmt, parser in MENU_ENTRIES[8:]:
            current = fmt(self.cfg)
            lines.append(f"    [{entry_key}]  {label:<22s} {current}")

        lines.append("")
        lines.append("  🔧  ACTIONS")
        lines.append("  " + "─" * 50)
        lines.append(f"    [P]  {'▶️  Resume Bot' if self.paused else '⏸️  Pause Bot'}")
        lines.append("    [R]  Reset Daily Stats")

        text = Text("\n".join(lines), style="white")
        return Panel(text, border_style="")

    def _menu_input_panel(self) -> Panel:
        """Panel shown when editing a specific setting."""
        # Find the selected menu entry
        entry = None
        for e in MENU_ENTRIES:
            if e[0] == self._menu_selected_key:
                entry = e
                break

        if entry is None:
            return Panel(Text("Unknown setting", style="bright_red"))

        _, label, path, getter, fmt, parser = entry
        current_display = fmt(self.cfg)

        lines = []
        lines.append("")
        lines.append(f"  ✏️   Editing: {label}")
        lines.append("  " + "─" * 50)
        lines.append(f"  Current value: {current_display}")
        lines.append("")
        lines.append(f"  New value: [bold bright_green]{self._menu_input_buffer}_[/bold bright_green]")
        lines.append("")

        if self._menu_message and time.time() - self._menu_msg_time < 2.0:
            lines.append(f"  {self._menu_message}")

        text = Text("\n".join(lines), style="white")
        return Panel(text, border_style="", title="✏️ EDIT SETTING", title_align="center")

    def _menu_footer_panel(self) -> Panel:
        """Footer with context-sensitive instructions."""
        text = Text()

        if self._menu_state == "input":
            text.append(" [Enter] Confirm  ", style="bold green on grey11")
            text.append(" [Esc] Cancel  ", style="bold red on grey11")
            text.append(" [Backspace] Delete  ", style="bold yellow on grey11")
        else:
            text.append(" [1-9] Edit Setting  ", style="bold cyan on grey11")
            text.append(" [P] Pause/Resume  ", style="bold yellow on grey11")
            text.append(" [R] Reset Daily  ", style="bold yellow on grey11")
            text.append(" [Q/Esc] Close Menu  ", style="bold red on grey11")

        if self._menu_message and time.time() - self._menu_msg_time < 3.0:
            # Already shown in body for input mode, or in header
            pass

        return Panel(text, border_style="dim")

    # ─── Dashboard Panels (existing, mostly unchanged) ───────────────────

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

        text = Text()
        text.append(" 🤖 ", style="bold")
        text.append("BOLLINGER BAND REVERSAL BOT", style="bold bright_cyan")
        text.append("  |  ", style="dim")
        text.append(f"Status: ", style="dim")
        text.append(f"{self.bot_status}", style=f"bold {status_color}")

        if self.paused:
            text.append("  |  ", style="dim")
            text.append("⏸️ PAUSED", style="bold bright_red")

        text.append("  |  ", style="dim")
        text.append("24/7 LIVE", style="bold green")
        text.append("  |  ", style="dim")
        text.append(f"{now.strftime('%Y-%m-%d %H:%M:%S')} IST", style="bold white")
        text.append("  |  ", style="dim")
        text.append(f"Uptime: {hours:02d}:{mins:02d}:{secs:02d}", style="cyan")

        if self.paper_trading:
            text.append("  |  ", style="dim")
            text.append("📝 PAPER", style="bold yellow")

        return Panel(
            Align.center(text),
            border_style="bright_cyan",
            box=box.HEAVY,
        )

    def _price_balance_panel(self) -> Panel:
        """Live price ticker and account balance."""
        layout = Layout()
        layout.split_row(
            Layout(name="price", ratio=2),
            Layout(name="balance", ratio=1),
        )

        # Price display
        price_text = Text()
        price_text.append("BTC/USDT  ", style="dim")
        if self.current_price > 0:
            direction = "▲" if self.current_price >= self.prev_price else "▼"
            dir_color = "bright_green" if self.current_price >= self.prev_price else "bright_red"
            price_text.append(f"{direction} ", style=f"bold {dir_color}")
            price_text.append(f"${self.current_price:,.2f}", style="bold bright_white")
        else:
            price_text.append("Loading...", style="dim yellow")

        layout["price"].update(Panel(price_text, title="💹 Live Price", border_style="green",
                                     title_align="left"))

        # Balance
        balance_table = Table(box=None, show_header=False, padding=(0, 1))
        balance_table.add_column("Asset", style="dim", width=6)
        balance_table.add_column("Amount", style="bold", justify="right")

        if self.balance:
            for asset, amount in sorted(self.balance.items()):
                if isinstance(amount, (int, float)) and amount > 0:
                    balance_table.add_row(asset, f"{amount:,.4f}")
        else:
            balance_table.add_row("USDT", "---")
            balance_table.add_row("BTC", "---")

        layout["balance"].update(Panel(balance_table, title="💰 Balance", border_style="green",
                                       title_align="left"))

        return Panel(layout, border_style="")

    def _position_panel(self) -> Panel:
        """Active position details."""
        if not self.position:
            return Panel(
                Align.center(Text("No active position", style="dim")),
                title="📊 Current Position",
                border_style="blue",
                title_align="left",
            )

        pos = self.position
        table = Table(box=None, show_header=False, padding=(0, 1))
        table.add_column("Label", style="dim", width=12)
        table.add_column("Value", style="bold")

        # Side
        side_color = "bright_green" if pos.side == "LONG" else "bright_red"
        table.add_row("Side", Text(pos.side, style=side_color))
        table.add_row("Entry Price", Text(f"${pos.entry_price:,.2f}", style="white"))
        table.add_row("Quantity", Text(f"{pos.quantity:,.6f} BTC", style="white"))
        table.add_row("Current Price", Text(f"${self.current_price:,.2f}", style="white"))

        # Unrealized P&L
        if pos.unrealized_pnl_pct != 0:
            pnl_color = "bright_green" if pos.unrealized_pnl_pct > 0 else "bright_red"
            pnl_sign = "+" if pos.unrealized_pnl_pct > 0 else ""
            table.add_row("Unreal. P&L %",
                         Text(f"{pnl_sign}{pos.unrealized_pnl_pct:.2f}%", style=f"bold {pnl_color}"))

        # Trailing stop
        table.add_row("Trail Stop", Text(f"${pos.trailing_stop:,.2f}", style="bright_yellow"))
        table.add_row("Highest Price", Text(f"${pos.highest_price:,.2f}", style="bright_green"))
        table.add_row("Lowest Price", Text(f"${pos.lowest_price:,.2f}", style="bright_green"))

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
        lines.append(f"  BB Std Dev:       {st.bb_stddev}")
        lines.append(f"  Near Threshold:   {st.near_threshold*100:.1f}%")
        lines.append(f"  Trail Stop:       {st.trail_pct*100:.1f}%")
        lines.append(f"  Trade Size:       ₹{rk.trade_size_inr:,.0f}")
        lines.append(f"  USD/INR Rate:     ₹{rk.usd_inr_rate}")
        lines.append(f"  Max Daily Loss:   ₹{rk.max_daily_loss_inr:,.0f}")
        lines.append(f"  Max Trades/Day:   {rk.max_trades_per_day}")
        lines.append(f"  Poll Interval:    {self.cfg.poll_interval_sec}s")
        lines.append(f"  Exchange:         SharkEx v1")
        lines.append(f"  Strategy:         24/7 BB Reversal")

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
        """Footer with cycle info, errors, hotkey hints, and session times."""
        text = Text()

        # Hotkey hints (prominent)
        text.append(" [M]enu  ", style="bold cyan on grey11")
        text.append(" [P]ause  ", style="bold yellow on grey11")
        text.append(" [H]elp  ", style="bold white on grey11")
        text.append(" [Ctrl+C] Quit  ", style="bold red on grey11")
        text.append("│  ", style="dim")

        text.append(f"Cycle: #{self.cycle_count}  |  ", style="dim")
        text.append(f"Interval: {self.cfg.poll_interval_sec}s  |  ", style="dim")
        text.append(f"24/7 Trading | BB({self.cfg.strategy.bb_period}, {self.cfg.strategy.bb_stddev}σ)  ", style="dim")

        # Flash message (pause/resume notification)
        if self._menu_message and time.time() - self._menu_msg_time < 2.5 and not self._menu_mode:
            text.append(f"\n  {self._menu_message}", style="bold yellow")

        if self.last_error:
            text.append(f"\n⚠️  Last Error: {self.last_error}", style="bold red")

        return Panel(text, box=box.SIMPLE, border_style="dim")

    # ─── Static Print Methods (for non-live mode / logging) ──────────────

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