"""
cli_display.py - Professional Terminal-Adaptive Live CLI Dashboard

Renders a clean, responsive trading dashboard that automatically adjusts
to terminal width.  On wide terminals (≥100 cols) panels are side-by-side;
on narrow terminals they stack vertically for perfect readability over SSH.

Uses Rich ``Live`` for flicker-free in-place updates — no screen clearing,
no flicker, no overlapping logs during menu editing.

Hotkeys (anytime):
  [M]  - Open settings menu (freezes dashboard, suppresses log noise)
  [P]  - Pause / Resume bot trading
  [H]  - Show help overlay (3s)

Menu hotkeys (when settings menu is open):
  [0-9, L] - Select a setting to edit
  [Enter]  - Confirm new value
  [Backspace] - Delete last character / go back
  [Esc/Q]  - Close menu and return to dashboard
  [R]      - Reset daily stats
  [P]      - Pause/Resume
"""

import os
import sys
import time
import queue
import shutil
import threading
import logging
from datetime import datetime
from typing import Optional, List, Dict, Any, Tuple

import pytz
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.layout import Layout
from rich.text import Text
from rich.align import Align
from rich.columns import Columns
from rich.live import Live
from rich import box

from config import BotConfig
from exchange_client import Position, OrderSide
from strategy import BBResult, StrategyConfig
from risk_manager import RiskManager, TradeRecord

logger = logging.getLogger("cli_display")

IST = pytz.timezone("Asia/Kolkata")

# ─── Responsive threshold ───
WIDE_MIN_WIDTH = 100   # cols needed for side-by-side layout
NARROW_MIN_WIDTH = 60  # below this we strip even more


# =============================================================================
# Keyboard Listener (cross-platform non-blocking stdin) — UNCHANGED
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
        return not self._queue.empty()

    def get_key(self) -> Optional[str]:
        try:
            return self._queue.get_nowait()
        except queue.Empty:
            return None

    def drain(self) -> List[str]:
        keys = []
        while not self._queue.empty():
            try:
                keys.append(self._queue.get_nowait())
            except queue.Empty:
                break
        return keys

    def start(self):
        if not sys.stdin.isatty():
            logger.debug("stdin is not a TTY; keyboard listener disabled")
            return
        self._running = True
        self._thread = threading.Thread(target=self._listen, daemon=True)
        self._thread.start()
        logger.debug("Keyboard listener started")

    def stop(self):
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=0.5)
        self._restore_terminal()
        logger.debug("Keyboard listener stopped")

    def _listen(self):
        try:
            if os.name == 'nt':
                self._listen_windows()
            else:
                self._listen_unix()
        except Exception as e:
            logger.debug(f"Keyboard listener error: {e}")

    def _listen_windows(self):
        import msvcrt
        while self._running:
            try:
                if msvcrt.kbhit():
                    ch = msvcrt.getch()
                    try:
                        key = ch.decode('utf-8', errors='ignore').lower()
                    except (UnicodeDecodeError, AttributeError):
                        if ch == b'\xe0':
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
        if self._old_termios is not None and self._fd is not None:
            import termios
            try:
                termios.tcsetattr(self._fd, termios.TCSADRAIN, self._old_termios)
                self._old_termios = None
            except (termios.error, IOError, OSError):
                pass


# =============================================================================
# Settings Menu Definition — UNCHANGED
# =============================================================================

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
    # Exchange
    ("0", "Trading Pair",        "exchange.symbol",
      lambda cfg: str(cfg.exchange.symbol),
      lambda cfg: f"{cfg.exchange.symbol}",
      lambda cfg, v: setattr(cfg.exchange, 'symbol', v.upper())),
    ("L", "Leverage",            "exchange.leverage",
      lambda cfg: str(cfg.exchange.leverage),
      lambda cfg: f"{cfg.exchange.leverage}x",
      lambda cfg, v: setattr(cfg.exchange, 'leverage', int(v))),
]


# =============================================================================
# CLIDisplay Class — TERMINAL-ADAPTIVE REWRITE
# =============================================================================

class CLIDisplay:
    """
    Professional, terminal-width-aware live CLI dashboard.

    Automatically switches between wide (side-by-side panels) and narrow
    (stacked vertical) layouts based on real terminal dimensions, making
    the dashboard look pristine over SSH connections of any size.
    """

    def __init__(self, cfg: BotConfig):
        self.cfg = cfg
        self.console = Console()
        self._start_time = time.time()

        # ── Keyboard / interaction ──
        self._keyboard = KeyboardListener()
        self._action_queue: queue.Queue = queue.Queue()
        self._menu_mode: bool = False
        self._menu_state: str = "main"
        self._menu_input_buffer: str = ""
        self._menu_selected_key: str = ""
        self._menu_message: str = ""
        self._menu_msg_time: float = 0.0
        self._help_overlay_until: float = 0.0

        # ── Live display engine (flicker-free in-place updates) ──
        self._live: Optional[Live] = None
        self._dirty: bool = False  # set True when keystroke needs immediate render

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

        # ── Nearest trade prediction ──
        self.nearest_trade_direction: str = ""
        self.nearest_trade_distance_pct: float = 0.0
        self.nearest_trade_trigger_price: float = 0.0

    # ─── Terminal width helper ────────────────────────────────────────────

    @property
    def _width(self) -> int:
        """Detect terminal width, falling back to Rich console size."""
        try:
            cols, _ = shutil.get_terminal_size(fallback=(80, 24))
            return max(60, cols)
        except Exception:
            return max(60, self.console.size.width)

    @property
    def _is_wide(self) -> bool:
        return self._width >= WIDE_MIN_WIDTH

    # ─── Public API ──────────────────────────────────────────────────────

    def update_data(self, **kwargs):
        """Update display data from the bot loop."""
        for key, value in kwargs.items():
            if hasattr(self, key):
                if key == "current_price" and self.current_price != 0:
                    self.prev_price = self.current_price
                setattr(self, key, value)

    @property
    def pending_actions(self) -> List[Tuple[str, Any]]:
        """Drain and return all pending actions for the bot to process."""
        actions = []
        while not self._action_queue.empty():
            try:
                actions.append(self._action_queue.get_nowait())
            except queue.Empty:
                break
        return actions

    # ─── Display Lifecycle ───────────────────────────────────────────────

    def start_live_display(self):
        """Start the live display (flicker-free in-place rendering) and keyboard
        listener.  ``Live`` overwrites the same terminal region on each update
        so there is no blank-flash and no log/display overlap."""
        self._keyboard.start()
        layout = self._generate_layout()
        self._live = Live(
            layout,
            console=self.console,
            refresh_per_second=20,
            transient=False,
            screen=False,
            vertical_overflow="visible",
        )
        self._live.start(refresh=True)

    def stop_live_display(self):
        """Stop the live display and keyboard listener."""
        self._keyboard.stop()
        if self._live:
            try:
                self._live.stop()
            except Exception:
                pass
            self._live = None

    def refresh(self):
        """Render the current layout in-place (no clear → no flicker)."""
        self._process_keystrokes()
        if self._live:
            try:
                self._live.update(self._generate_layout(), refresh=True)
            except Exception:
                pass  # Live may have been stopped during shutdown

    def _render_now(self):
        """Force an immediate in-place re-render (used mid-cycle for
        keystroke echo during menu input)."""
        if self._live:
            try:
                self._live.update(self._generate_layout(), refresh=True)
            except Exception:
                pass

    def tick(self):
        """Lightweight tick: process keystrokes only, no render."""
        self._process_keystrokes()

    def shutdown(self):
        """Full cleanup."""
        self._keyboard.stop()
        if self._live:
            try:
                self._live.stop()
            except Exception:
                pass
            self._live = None

    # ─── Keyboard / Interaction Handling — UNCHANGED ────────────────────

    def _process_keystrokes(self):
        keys = self._keyboard.drain()
        if not keys:
            return
        for key in keys:
            self._handle_key(key)
        # During menu input mode, re-render immediately so keystrokes
        # appear on screen without waiting for the next bot cycle.
        if self._menu_mode:
            self._render_now()

    def _handle_key(self, key: str):
        # ── Global hotkeys ──
        if key == 'm' and not self._menu_mode:
            self._menu_mode = True
            self._menu_state = "main"
            self._menu_selected_key = ""
            self._menu_input_buffer = ""
            self._menu_message = ""
            return

        if key == 'p' and not self._menu_mode:
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

        # ── Menu keys ──
        if not self._menu_mode:
            return

        if key in ('<esc>', 'q'):
            self._menu_mode = False
            self._menu_state = "main"
            self._menu_input_buffer = ""
            self._menu_selected_key = ""
            self._menu_message = ""
            return

        if key == 'r':
            self._action_queue.put(("reset_daily", None))
            self._menu_message = "✅ Daily stats RESET"
            self._menu_msg_time = time.time()
            return

        if key == 'p' and self._menu_mode:
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
                    self._menu_state = "main"
                    self._menu_selected_key = ""
                return
            elif len(key) == 1 and key.isprintable():
                self._menu_input_buffer += key
                return
            else:
                return

        # ── Main menu mode ──
        if self._menu_state == "main":
            key_lower = key.lower()
            for entry_key, label, path, getter, fmt, parser in MENU_ENTRIES:
                if key_lower == entry_key.lower():
                    self._menu_state = "input"
                    self._menu_selected_key = entry_key
                    self._menu_input_buffer = getter(self.cfg)
                    return

    def _confirm_input(self):
        """Validate and apply the input buffer value to the config."""
        if not self._menu_selected_key:
            self._menu_state = "main"
            return

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

        self._menu_state = "main"
        self._menu_selected_key = ""
        self._menu_input_buffer = ""

    # ═════════════════════════════════════════════════════════════════════
    #  LAYOUT GENERATION (terminal-adaptive)
    # ═════════════════════════════════════════════════════════════════════

    def _generate_layout(self) -> Layout:
        """Generate layout — dashboard, menu, or help overlay."""
        if self._menu_mode:
            return self._generate_menu_layout()
        elif self._help_overlay_until > time.time():
            return self._generate_help_layout()
        else:
            return self._generate_dashboard_layout()

    def _generate_dashboard_layout(self) -> Layout:
        """Terminal-adaptive trading dashboard.

        Wide (≥100 cols):  price+balance left | position | strategy
                            P&L+signal left     | trades
                            — side-by-side columns, professional density.

        Narrow (<100 cols): stacked single-column, every panel full-width.
        """
        layout = Layout()
        w = self._width

        if self._is_wide:
            # ── WIDE: two-column professional layout ──
            layout.split_column(
                Layout(name="header",  size=3),
                Layout(name="top_row"),
                Layout(name="bottom_row"),
                Layout(name="footer",  size=3),
            )
            layout["top_row"].split_row(
                Layout(name="price_balance", ratio=3),
                Layout(name="position",      ratio=2),
                Layout(name="strategy_config", ratio=2),
            )
            layout["bottom_row"].split_row(
                Layout(name="pnl_signal", ratio=1),
                Layout(name="trades",     ratio=1),
            )
        else:
            # ── NARROW: single-column stacked ──
            layout.split_column(
                Layout(name="header",  size=3),
                Layout(name="price_balance"),
                Layout(name="position"),
                Layout(name="pnl_signal"),
                Layout(name="strategy_config"),
                Layout(name="trades"),
                Layout(name="footer",  size=3),
            )

        # Fill every region — panels auto-adapt internally
        layout["header"].update(self._header_panel())
        layout["price_balance"].update(self._price_balance_panel())
        layout["position"].update(self._position_panel())
        layout["strategy_config"].update(self._strategy_config_panel())
        layout["pnl_signal"].update(self._pnl_signal_panel())
        layout["trades"].update(self._trades_panel())
        layout["footer"].update(self._footer_panel())

        return layout

    # ─── Help Overlay — UNCHANGED ───────────────────────────────────────

    def _generate_help_layout(self) -> Layout:
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
        help_text.append("  [0-9,L]  Select Setting  ", style="bold yellow")
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

    # ─── Menu Layout — UNCHANGED ────────────────────────────────────────

    def _generate_menu_layout(self) -> Layout:
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
        text = Text()
        text.append(" ⚙️  ", style="bold")
        text.append("SETTINGS MENU", style="bold bright_cyan")
        if self._menu_message and (time.time() - self._menu_msg_time) < 2.0:
            text.append("  —  ")
            text.append(self._menu_message, style="bold yellow")
        if self.paused:
            text.append("  |  ")
            text.append("⏸️ PAUSED", style="bold bright_red")
        return Panel(text, border_style="bright_cyan", title_align="left")

    def _menu_body_panel(self) -> Panel:
        if self._menu_state == "input":
            return self._menu_input_panel()

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
        lines.append("  🏦  EXCHANGE")
        lines.append("  " + "─" * 50)

        for entry_key, label, path, getter, fmt, parser in MENU_ENTRIES[9:11]:
            current = fmt(self.cfg)
            lines.append(f"    [{entry_key}]  {label:<22s} {current}")

        lines.append("")
        lines.append("  ⏱️   POLLING")
        lines.append("  " + "─" * 50)

        for entry_key, label, path, getter, fmt, parser in MENU_ENTRIES[8:9]:
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

        if self._menu_message and (time.time() - self._menu_msg_time) < 2.0:
            lines.append(f"  {self._menu_message}")

        text = Text("\n".join(lines), style="white")
        return Panel(text, border_style="", title="✏️ EDIT SETTING", title_align="center")

    def _menu_footer_panel(self) -> Panel:
        text = Text()
        if self._menu_state == "input":
            text.append(" [Enter] Confirm  ", style="bold green on grey11")
            text.append(" [Esc] Cancel  ", style="bold red on grey11")
            text.append(" [Backspace] Delete  ", style="bold yellow on grey11")
        else:
            text.append(" [0-9,L] Edit Setting  ", style="bold cyan on grey11")
            text.append(" [P] Pause/Resume  ", style="bold yellow on grey11")
            text.append(" [R] Reset Daily  ", style="bold yellow on grey11")
            text.append(" [Q/Esc] Close Menu  ", style="bold red on grey11")
        return Panel(text, border_style="dim")


    # ═════════════════════════════════════════════════════════════════════
    #  DASHBOARD PANELS (all terminal-width-aware)
    # ═════════════════════════════════════════════════════════════════════

    def _header_panel(self) -> Panel:
        """Header bar — adapts to terminal width by compacting labels."""
        now = datetime.now(IST)
        runtime = time.time() - self._start_time
        hours = int(runtime // 3600)
        mins = int((runtime % 3600) // 60)
        secs = int(runtime % 60)

        status_color = "green" if self.bot_status == "RUNNING" else (
            "red" if "ERROR" in self.bot_status else "yellow"
        )

        w = self._width
        compact = w < 85

        text = Text()
        text.append(" 🤖 ", style="bold")
        if compact:
            text.append("BB REVERSAL BOT", style="bold bright_cyan")
        else:
            text.append("BOLLINGER BAND REVERSAL BOT", style="bold bright_cyan")

        text.append("  │  ", style="dim")

        if compact:
            text.append(f"{self.bot_status[:6]}", style=f"bold {status_color}")
        else:
            text.append(f"Status: ", style="dim")
            text.append(f"{self.bot_status}", style=f"bold {status_color}")

        if self.paused:
            text.append(" ⏸", style="bold bright_red")

        if not compact:
            text.append("  │  ", style="dim")
            text.append("24/7 LIVE", style="bold green")
            text.append("  │  ", style="dim")
            text.append(f"{now.strftime('%H:%M:%S')} IST", style="bold white")
            text.append("  │  ", style="dim")
            text.append(f"Up {hours:02d}:{mins:02d}:{secs:02d}", style="cyan")
        else:
            text.append("  │  ", style="dim")
            text.append(f"{now.strftime('%H:%M:%S')}", style="bold white")
            text.append("  │  ", style="dim")
            text.append(f"{hours:02d}:{mins:02d}:{secs:02d}", style="cyan")

        if self.paper_trading:
            text.append("  │  ", style="dim")
            text.append("PAPER", style="bold yellow")

        return Panel(
            Align.center(text),
            border_style="bright_cyan",
            box=box.HEAVY,
        )

    def _price_balance_panel(self) -> Panel:
        """Price ticker + balance — side-by-side on wide, stacked on narrow."""
        w = self._width

        if self._is_wide:
            # ── WIDE: price and balance in one horizontal panel ──
            inner = Layout()
            inner.split_row(
                Layout(name="price", ratio=3),
                Layout(name="balance", ratio=2),
            )

            # Price block
            price_text = Text()
            price_text.append("BTC/USDT  ", style="dim")
            if self.current_price > 0:
                direction = "▲" if self.current_price >= self.prev_price else "▼"
                dir_color = "bright_green" if self.current_price >= self.prev_price else "bright_red"
                price_text.append(f"{direction} ", style=f"bold {dir_color}")
                price_text.append(f"${self.current_price:,.2f}", style="bold bright_white")
            else:
                price_text.append("Loading...", style="dim yellow")
            inner["price"].update(Panel(price_text, title="💹 Live Price",
                                        border_style="green", title_align="left"))

            # Balance block
            balance_table = self._build_balance_table()
            inner["balance"].update(Panel(balance_table, title="💰 Balance",
                                          border_style="green", title_align="left"))
            return Panel(inner, border_style="")
        else:
            # ── NARROW: price and balance as separate rows in one panel ──
            lines = []
            if self.current_price > 0:
                direction = "▲" if self.current_price >= self.prev_price else "▼"
                dir_mark = "green" if self.current_price >= self.prev_price else "red"
                lines.append(Text.assemble(
                    ("BTC/USDT  ", "dim"),
                    (f"{direction} ${self.current_price:,.2f}", f"bold {dir_mark}"),
                ))
            else:
                lines.append(Text("BTC/USDT  Loading...", style="dim yellow"))

            lines.append(Text(""))  # spacer

            # Compact balance: "INR ₹1,941.50  |  USDT $22.31"
            bal_parts = []
            for asset, amount in sorted(self.balance.items()):
                if isinstance(amount, (int, float)) and amount > 0:
                    if asset == "INR":
                        bal_parts.append(f"INR ₹{amount:,.2f}")
                    elif amount >= 100:
                        bal_parts.append(f"{asset} {amount:,.2f}")
                    else:
                        bal_parts.append(f"{asset} {amount:,.4f}")
            if bal_parts:
                lines.append(Text("  │  ".join(bal_parts), style="bold white"))
            else:
                lines.append(Text("Balance: ---", style="dim"))

            # BB band summary (compact)
            if self.bb_result and self.bb_result.sma > 0:
                bb = self.bb_result
                lines.append(Text(""))
                lines.append(Text.assemble(
                    ("BB: ", "dim"),
                    (f"U ${bb.upper:,.2f}  ", "bright_red"),
                    (f"M ${bb.sma:,.2f}  ", "white"),
                    (f"L ${bb.lower:,.2f}  ", "bright_green"),
                    (f"W ${bb.width:,.2f}", "dim"),
                ))

            contents = Text("\n").join(lines) if len(lines) > 1 else lines[0]
            return Panel(contents, title="💹 Price & Balance", border_style="green",
                         title_align="left")

    def _build_balance_table(self) -> Table:
        """Shared balance table builder used by wide layout."""
        table = Table(box=None, show_header=False, padding=(0, 1))
        table.add_column("Asset", style="dim", width=6)
        table.add_column("Amount", style="bold", justify="right")

        if self.balance:
            shown = 0
            for asset, amount in sorted(self.balance.items()):
                if isinstance(amount, (int, float)) and amount > 0:
                    if asset == "INR":
                        table.add_row(asset, f"₹{amount:,.2f}")
                    elif amount >= 100:
                        table.add_row(asset, f"{amount:,.2f}")
                    else:
                        table.add_row(asset, f"{amount:,.4f}")
                    shown += 1
            if shown == 0:
                table.add_row("---", "Empty")
        else:
            table.add_row("---", "Loading")
        return table

    def _position_panel(self) -> Panel:
        """Active position — detailed table on wide, summary line on narrow."""
        if not self.position:
            return Panel(
                Align.center(Text("No active position", style="dim")),
                title="📊 Position",
                border_style="blue",
                title_align="left",
            )

        pos = self.position
        w = self._width

        if self._is_wide:
            table = Table(box=None, show_header=False, padding=(0, 1))
            table.add_column("", style="dim", width=14)
            table.add_column("", style="bold")

            side_color = "bright_green" if pos.side == "LONG" else "bright_red"
            table.add_row("Side", Text(pos.side, style=side_color))
            table.add_row("Entry Price", f"${pos.entry_price:,.2f}")
            table.add_row("Quantity", f"{pos.quantity:,.6f} BTC")
            table.add_row("Current Price", f"${self.current_price:,.2f}")

            if pos.unrealized_pnl_pct != 0:
                pnl_color = "bright_green" if pos.unrealized_pnl_pct > 0 else "bright_red"
                pnl_sign = "+" if pos.unrealized_pnl_pct > 0 else ""
                table.add_row("Unreal. P&L %",
                             Text(f"{pnl_sign}{pos.unrealized_pnl_pct:.2f}%",
                                  style=f"bold {pnl_color}"))

            table.add_row("Trail Stop", f"${pos.trailing_stop:,.2f}")
            table.add_row("Peak High", f"${pos.highest_price:,.2f}")
            table.add_row("Peak Low", f"${pos.lowest_price:,.2f}")

            entry_dt = datetime.fromtimestamp(pos.entry_time, tz=IST)
            table.add_row("Entry Time", entry_dt.strftime("%H:%M:%S IST"))

            return Panel(table, title="📊 Current Position", border_style="blue",
                         title_align="left")
        else:
            # ── Narrow: compact two-line format ──
            side_color = "bright_green" if pos.side == "LONG" else "bright_red"
            pnl_pct = pos.unrealized_pnl_pct
            pnl_color = "bright_green" if pnl_pct >= 0 else "bright_red"
            pnl_sign = "+" if pnl_pct >= 0 else ""

            lines = []
            lines.append(Text.assemble(
                (f"{pos.side} ", f"bold {side_color}"),
                (f"@{pos.entry_price:,.2f}  ", "white"),
                (f"Qty: {pos.quantity:,.6f} BTC  ", "dim"),
                (f"Now: ${self.current_price:,.2f}", "white"),
            ))
            lines.append(Text.assemble(
                (f"Trail: ${pos.trailing_stop:,.2f}  ", "bright_yellow"),
                (f"P&L: {pnl_sign}{pnl_pct:.2f}%  ", f"bold {pnl_color}"),
                (f"Hi: ${pos.highest_price:,.2f}  ", "bright_green"),
                (f"Lo: ${pos.lowest_price:,.2f}", "bright_green"),
            ))

            text = Text("\n").join(lines)
            return Panel(text, title="📊 Current Position", border_style="blue",
                         title_align="left")

    def _strategy_config_panel(self) -> Panel:
        """Strategy config — left-aligned rows; denser on narrow terminals."""
        st = self.cfg.strategy
        rk = self.cfg.risk
        w = self._width

        if self._is_wide:
            lines = [
                f"  BB Period:        {st.bb_period}",
                f"  BB Std Dev:       {st.bb_stddev}",
                f"  Near Threshold:   {st.near_threshold*100:.1f}%",
                f"  Trail Stop:       {st.trail_pct*100:.1f}%",
                f"  Trade Size:       ₹{rk.trade_size_inr:,.0f}",
                f"  USD/INR Rate:     ₹{rk.usd_inr_rate}",
                f"  Max Daily Loss:   ₹{rk.max_daily_loss_inr:,.0f}",
                f"  Max Trades/Day:   {rk.max_trades_per_day}",
                f"  Poll Interval:    {self.cfg.poll_interval_sec}s",
                f"  Exchange:         SharkEx v1",
                f"  Strategy:         24/7 BB Reversal",
            ]
        else:
            # Compact: 3 columns of small items
            lines = [
                f"BB({st.bb_period},{st.bb_stddev}σ)  Near:{st.near_threshold*100:.1f}%  Trail:{st.trail_pct*100:.1f}%",
                f"Trade: ₹{rk.trade_size_inr:,.0f}  USD/INR: ₹{rk.usd_inr_rate}  MaxLoss: ₹{rk.max_daily_loss_inr:,.0f}",
                f"Max {rk.max_trades_per_day}/day  Poll: {self.cfg.poll_interval_sec}s  SharkEx v1",
            ]

        text = Text("\n".join(lines), style="cyan")
        return Panel(text, title="⚙️ Strategy Config", border_style="magenta",
                     title_align="left")

    def _pnl_signal_panel(self) -> Panel:
        """P&L + Signal — side-by-side on wide, stacked on narrow."""
        if self._is_wide:
            inner = Layout()
            inner.split_row(
                Layout(name="pnl"),
                Layout(name="signal"),
            )

            # P&L sub-panel
            pnl_table = Table(box=None, show_header=False, padding=(0, 1))
            pnl_table.add_column("", style="dim", width=16)
            pnl_table.add_column("", style="bold")

            if self.risk_manager:
                rm = self.risk_manager
                pnl_usdt_color = "bold bright_green" if rm.daily_pnl_usdt >= 0 else "bold bright_red"
                pnl_inr_color = "bold bright_green" if rm.daily_pnl_inr >= 0 else "bold bright_red"
                pnl_table.add_row("Daily P&L (USDT)",
                                 Text(f"${rm.daily_pnl_usdt:+,.2f}", style=pnl_usdt_color))
                pnl_table.add_row("Daily P&L (INR)",
                                 Text(f"₹{rm.daily_pnl_inr:+,.2f}", style=pnl_inr_color))
                pnl_table.add_row("Trades Today",
                                 f"{rm.trades_today}/{self.cfg.risk.max_trades_per_day}")
                pnl_table.add_row("Max Loss Limit",
                                 f"₹{self.cfg.risk.max_daily_loss_inr:,.0f}")

                if rm.is_locked:
                    pnl_table.add_row("", "")
                    pnl_table.add_row("STATUS", Text("🔒 LOCKED", style="bold bright_red"))
                    pnl_table.add_row("Reason", Text(rm.lock_reason, style="red"))
                else:
                    pnl_table.add_row("STATUS", Text("✅ ACTIVE", style="bold bright_green"))

            inner["pnl"].update(Panel(pnl_table, title="📈 P&L Summary",
                                       border_style="green", title_align="left"))

            # Signal sub-panel
            sig = self._build_signal_subpanel()
            inner["signal"].update(Panel(sig, title="🎯 Signal & BB",
                                          border_style="yellow", title_align="left"))

            return Panel(inner, border_style="")
        else:
            # ── Narrow: stacked P&L then signal ──
            lines = []
            if self.risk_manager:
                rm = self.risk_manager
                pnl_color = "green" if rm.daily_pnl_inr >= 0 else "red"
                pnl_sign = "+" if rm.daily_pnl_inr >= 0 else ""
                status = "🔒LOCKED" if rm.is_locked else "✅ACTIVE"
                lines.append(
                    f"P&L: {pnl_sign}₹{rm.daily_pnl_inr:,.2f}  "
                    f"Trades: {rm.trades_today}/{self.cfg.risk.max_trades_per_day}  "
                    f"{status}"
                )
                if rm.is_locked:
                    lines.append(f"Lock: {rm.lock_reason}")

            # Signal
            signal_color = {"LONG": "bright_green", "SHORT": "bright_red",
                           "NONE": "dim"}.get(self.signal, "white")
            signal_emoji = {"LONG": "🟢", "SHORT": "🔴", "NONE": "⚪"}.get(self.signal, "⚪")
            lines.append(f"Signal: {signal_emoji} {self.signal}")
            if self.signal_reason:
                lines.append(f"  {self.signal_reason}")

            # ── Nearest trade prediction (narrow) ──
            if self.nearest_trade_trigger_price > 0 and self.nearest_trade_direction:
                n_dir = self.nearest_trade_direction
                n_emoji = "🟢" if n_dir == "LONG" else "🔴"
                n_pct = self.nearest_trade_distance_pct
                n_price = self.nearest_trade_trigger_price
                lines.append(
                    f"Next Trade: {n_emoji} {n_dir} @ ${n_price:,.2f} ({n_pct:.2f}%)"
                )

            if self.bb_result and self.bb_result.sma > 0:
                bb = self.bb_result
                lines.append(
                    f"BB: U ${bb.upper:,.2f}  M ${bb.sma:,.2f}  L ${bb.lower:,.2f}  "
                    f"W ${bb.width:,.2f}"
                )

            return Panel(Text("\n".join(lines), style="white"),
                         title="📈 P&L & Signal", border_style="green",
                         title_align="left")

    def _build_signal_subpanel(self) -> Table:
        """Shared signal/BB table used by wide layout."""
        table = Table(box=None, show_header=False, padding=(0, 1))
        table.add_column("", style="dim", width=16)
        table.add_column("", style="bold")

        signal_color = {"LONG": "bold bright_green", "SHORT": "bold bright_red",
                       "NONE": "dim white"}.get(self.signal, "white")
        signal_emoji = {"LONG": "🟢", "SHORT": "🔴", "NONE": "⚪"}.get(self.signal, "⚪")

        table.add_row("Last Signal",
                     Text(f"{signal_emoji} {self.signal}", style=signal_color))
        if self.last_signal_distance:
            table.add_row("Band Distance",
                         f"{self.last_signal_distance:.3f}%")
        if self.signal_reason:
            table.add_row("Reason", Text(self.signal_reason, style="dim"))

        # ── Nearest trade prediction row ──
        if self.nearest_trade_trigger_price > 0 and self.nearest_trade_direction:
            n_dir = self.nearest_trade_direction
            n_emoji = "🟢" if n_dir == "LONG" else "🔴"
            n_color = "bold bright_green" if n_dir == "LONG" else "bold bright_red"
            table.add_row("", "")
            table.add_row("Next Trade",
                         Text(
                             f"{n_emoji} {n_dir} @ ${self.nearest_trade_trigger_price:,.2f} "
                             f"({self.nearest_trade_distance_pct:.2f}%)",
                             style=n_color,
                         ))

        if self.bb_result and self.bb_result.sma > 0:
            bb = self.bb_result
            table.add_row("", "")
            table.add_row("BB Upper", Text(f"${bb.upper:,.2f}", style="bright_red"))
            table.add_row("BB SMA",   Text(f"${bb.sma:,.2f}", style="white"))
            table.add_row("BB Lower", Text(f"${bb.lower:,.2f}", style="bright_green"))
            table.add_row("BB Width", f"${bb.width:,.2f}")
            table.add_row("Volatility σ", f"${bb.volatility:,.2f}")

        return table

    def _trades_panel(self) -> Panel:
        """Recent trades — full table on wide, compact on narrow."""
        w = self._width

        if self._is_wide:
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
        else:
            # Narrow: drop Exit and Reason columns to fit
            table = Table(
                box=box.SIMPLE,
                show_header=True,
                header_style="bold cyan",
                padding=(0, 1),
            )
            table.add_column("#", style="dim", width=3)
            table.add_column("Side", width=5)
            table.add_column("Entry", width=10)
            table.add_column("Qty", width=7)
            table.add_column("PnL (₹)", width=12)

        if not self.recent_trades:
            table.add_row("", "", "", "", "No trades yet", "", "")
        else:
            displayed = self.recent_trades[-10:][::-1]
            for t in displayed:
                side_color = "bright_green" if t.side in ("LONG", "BUY") else "bright_red"
                pnl_color = "bright_green" if t.pnl_usdt > 0 else "bright_red"
                pnl_sign = "+" if t.pnl_usdt >= 0 else ""

                if self._is_wide:
                    table.add_row(
                        str(t.id),
                        Text(t.side, style=side_color),
                        f"${t.entry_price:,.2f}",
                        f"${t.exit_price:,.2f}",
                        f"{t.quantity:,.6f}",
                        Text(f"{pnl_sign}₹{t.pnl_inr:,.2f}", style=pnl_color),
                        t.exit_reason,
                    )
                else:
                    table.add_row(
                        str(t.id),
                        Text(t.side, style=side_color),
                        f"${t.entry_price:,.2f}",
                        f"{t.quantity:,.6f}",
                        Text(f"{pnl_sign}₹{t.pnl_inr:,.2f}", style=pnl_color),
                    )

        max_rows = 12 if self._is_wide else 8
        return Panel(table, title=f"📋 Recent Trades (last {max_rows})",
                     border_style="yellow", title_align="left")

    def _footer_panel(self) -> Panel:
        """Footer — hotkey bar + cycle info; adapts to width."""
        w = self._width

        text = Text()

        if w >= 90:
            text.append(" [M]enu  ", style="bold cyan on grey11")
            text.append(" [P]ause  ", style="bold yellow on grey11")
            text.append(" [H]elp  ", style="bold white on grey11")
            text.append(" [Ctrl+C] Quit  ", style="bold red on grey11")
            text.append("│  ", style="dim")
            text.append(f"Cycle #{self.cycle_count}  │  ", style="dim")
            text.append(f"{self.cfg.poll_interval_sec}s  │  ", style="dim")
            text.append(f"BB({self.cfg.strategy.bb_period},{self.cfg.strategy.bb_stddev}σ)", style="dim")
        else:
            text.append(" [M]enu  [P]ause  [H]elp  [Ctrl+C] Quit", style="bold cyan on grey11")
            text.append(f"  │  Cycle #{self.cycle_count}  {self.cfg.poll_interval_sec}s",
                        style="dim")

        # Flash message
        if self._menu_message and (time.time() - self._menu_msg_time) < 2.5 and not self._menu_mode:
            text.append(f"\n  {self._menu_message}", style="bold yellow")

        if self.last_error:
            text.append(f"\n⚠️  {self.last_error}", style="bold red")

        return Panel(text, box=box.SIMPLE, border_style="dim")

    # ─── Static Print Methods ────────────────────────────────────────────

    def print_status_line(self, msg: str, style: str = "white"):
        """Print a single status line (suppressed during menu mode)."""
        if self._menu_mode:
            return  # Don't pollute the menu screen with log lines
        timestamp = datetime.now(IST).strftime("%H:%M:%S")
        self.console.print(f"[dim]{timestamp}[/dim] {msg}", style=style)

    def print_trade_executed(self, side: str, entry: float, qty: float,
                             usdt_val: float, inr_val: float):
        """Print trade execution details (suppressed during menu mode)."""
        if self._menu_mode:
            return
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
        """Print trade exit details (suppressed during menu mode)."""
        if self._menu_mode:
            return
        pnl_color = "bright_green" if pnl_usdt >= 0 else "bright_red"
        pnl_sign = "+" if pnl_usdt >= 0 else ""
        self.print_status_line(
            f"❌ [bold]POSITION CLOSED[/bold] | {reason} | "
            f"Exit: ${exit_price:,.2f} | "
            f"P&L: {pnl_sign}${pnl_usdt:,.2f} ({pnl_sign}₹{pnl_inr:,.2f})",
            style=pnl_color
        )

    def print_signal_detected(self, signal: str, reason: str):
        """Print detected signal (suppressed during menu mode)."""
        if signal == "NONE" or self._menu_mode:
            return
        color = "bright_green" if signal == "LONG" else "bright_red"
        emoji = "🟢" if signal == "LONG" else "🔴"
        self.print_status_line(
            f"{emoji} [bold {color}]SIGNAL: {signal}[/bold {color}] | {reason}",
            style="bold"
        )

    def print_error(self, error_msg: str):
        """Print error message (suppressed during menu mode)."""
        if self._menu_mode:
            return
        self.print_status_line(f"⚠️ [bold red]ERROR:[/bold red] {error_msg}", style="red")