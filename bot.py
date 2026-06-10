"""
bot.py - BB Squeeze Breakout Trading Bot (Main Engine)

Orchestrates all modules:
- SharkEx v1 exchange connection & data fetching
- Strategy signal detection (Bollinger Squeeze Breakout, 15-min candles)
- Order execution (limit + market fallback) & trailing stop management
- Risk management enforcement
- Live CLI display
- Web dashboard (port 8080)

Usage:
    python bot.py          # Interactive setup (asks for API keys)
    python bot.py --no-interactive  # Use .env or defaults, no prompts
"""

import os
import sys
import time
import signal
import logging
import logging.handlers
import traceback
import atexit
from datetime import datetime
from typing import Optional, Dict, Any, List

import pytz

from config import BotConfig
from exchange_client import (
    create_exchange_client,
    SharkExClient,
    Position,
    OrderSide,
    Order,
    OrderStatus,
)
from strategy import BollingerBandStrategy, BBResult, SignalResult
from risk_manager import RiskManager, TradeRecord
from cli_display import CLIDisplay
import web_api

# ─── Logging Setup ───
logger = logging.getLogger("bot")

IST = pytz.timezone("Asia/Kolkata")

# ─── Daemon PID File ───
PID_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".bot.pid")

# Trading session windows in IST (Asia/Kolkata), 3 sessions per day
SESSION_HOURS: List[tuple] = [
    (9, 30, 12, 0),    # 09:30 - 12:00 IST
    (13, 0, 15, 30),   # 13:00 - 15:30 IST
    (19, 0, 22, 0),    # 19:00 - 22:00 IST
]


def daemonize():
    """Double-fork to detach from the controlling terminal (Unix daemon)."""
    # First fork — detach from parent
    try:
        pid = os.fork()
        if pid > 0:
            sys.exit(0)  # Parent exits
    except OSError as e:
        sys.stderr.write(f"First fork failed: {e}\n")
        sys.exit(1)

    # Decouple from parent environment
    os.chdir("/")
    os.setsid()
    os.umask(0o027)

    # Second fork — relinquish session leadership
    try:
        pid = os.fork()
        if pid > 0:
            sys.exit(0)
    except OSError as e:
        sys.stderr.write(f"Second fork failed: {e}\n")
        sys.exit(1)

    # Redirect standard file descriptors to /dev/null
    devnull = os.open(os.devnull, os.O_RDWR)
    os.dup2(devnull, sys.stdin.fileno())
    os.dup2(devnull, sys.stdout.fileno())
    os.dup2(devnull, sys.stderr.fileno())
    os.close(devnull)

    # Write PID file
    with open(PID_FILE, "w") as f:
        f.write(str(os.getpid()))

    # Clean up PID file on exit
    atexit.register(lambda: os.path.exists(PID_FILE) and os.remove(PID_FILE))


def setup_file_logging(log_dir: str = None):
    """Configure rotating file handlers + console handler for persistent logging."""
    if log_dir is None:
        log_dir = os.path.dirname(os.path.abspath(__file__))

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)

    fmt = logging.Formatter(
        "[%(asctime)s] [%(levelname)-7s] [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # Simpler format for console (no timestamps — less noise in TUI mode)
    console_fmt = logging.Formatter(
        "[%(levelname)-7s] [%(name)s] %(message)s"
    )

    # General log — all levels
    general = logging.handlers.RotatingFileHandler(
        os.path.join(log_dir, "bot.log"),
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
    )
    general.setLevel(logging.INFO)
    general.setFormatter(fmt)
    root_logger.addHandler(general)

    # Error log — WARNING and above
    error = logging.handlers.RotatingFileHandler(
        os.path.join(log_dir, "bot_error.log"),
        maxBytes=2 * 1024 * 1024,
        backupCount=3,
    )
    error.setLevel(logging.WARNING)
    error.setFormatter(fmt)
    root_logger.addHandler(error)

    # Trade log — trade-specific events
    trade = logging.handlers.RotatingFileHandler(
        os.path.join(log_dir, "bot_trades.log"),
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
    )
    trade.setLevel(logging.INFO)
    trade.setFormatter(fmt)
    trade.addFilter(lambda record: record.name == "bot.trade")
    root_logger.addHandler(trade)

    # Console handler — INFO+ to stderr (visible even during Rich TUI)
    console = logging.StreamHandler(sys.stderr)
    console.setLevel(logging.INFO)
    console.setFormatter(console_fmt)
    root_logger.addHandler(console)

    logger.info("Logging initialized — bot.log, bot_error.log, bot_trades.log, + console")


# =============================================================================
# Main Trading Bot
# =============================================================================

class TradingBot:
    """
    Live spot trading bot using BB Squeeze Breakout on 15-min candles.

    Trades during 3 IST sessions: 09:30-12:00, 13:00-15:30, 19:00-22:00.
    Exits and data fetching run 24/7.

    Entry: limit order at bid, market fallback after 10 seconds.
    Exit:  trailing stop only (no take-profit).
    """

    def __init__(self, cfg: BotConfig):
        self.cfg = cfg
        self.exchange = create_exchange_client(cfg)
        self.strategy = BollingerBandStrategy(cfg.strategy)
        self.risk_manager = RiskManager(cfg.risk)
        self.display = CLIDisplay(cfg)

        # State
        self.position: Optional[Position] = None
        self.current_price: float = 0.0
        self.current_bb: Optional[BBResult] = None
        self.cycle_count: int = 0
        self.running: bool = True
        self.paused: bool = False
        self.last_error: str = ""

        # Web log buffer (circular, max 200 entries)
        self.web_logs: List[Dict[str, str]] = []
        self._trade_logger = logging.getLogger("bot.trade")

        # Register web API config callback
        web_api.set_config_callback(self._handle_web_config)

        # Register signal handlers for graceful shutdown
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

    def _signal_handler(self, signum, frame):
        """Handle graceful shutdown."""
        logger.info(f"\nSignal {signum} received. Shutting down gracefully...")
        self.running = False

    def _add_web_log(self, msg: str, cls: str = ""):
        """Add a log entry to the web dashboard circular buffer."""
        now = datetime.now(IST)
        entry = {
            "time": now.strftime("%H:%M:%S"),
            "msg": msg,
            "cls": cls,
        }
        self.web_logs.append(entry)
        if len(self.web_logs) > 200:
            self.web_logs = self.web_logs[-200:]

    @staticmethod
    def _is_in_trading_session() -> bool:
        """Check if current IST time falls within any trading session window."""
        now = datetime.now(IST)
        minutes = now.hour * 60 + now.minute
        for start_h, start_m, end_h, end_m in SESSION_HOURS:
            start = start_h * 60 + start_m
            end = end_h * 60 + end_m
            if start <= minutes < end:
                return True
        return False

    def _round_quantity(self, quantity: float) -> float:
        """Round quantity to appropriate precision for the exchange.
        BTC/USDT typically uses 6 decimal places."""
        return round(quantity, 6)

    # ─── DATA FETCHING ───

    def fetch_data(self) -> bool:
        """
        Fetch OHLCV data and current price from exchange.
        Returns True if successful.
        """
        try:
            # Fetch OHLCV
            ohlcv = self.exchange.fetch_ohlcv(
                timeframe=self.cfg.strategy.candle_tf,
                limit=self.cfg.strategy.data_window,
            )
            if not ohlcv or len(ohlcv) < self.cfg.strategy.bb_period:
                logger.warning(f"Insufficient OHLCV data: {len(ohlcv) if ohlcv else 0} candles")
                return False

            # Prepare DataFrame and calculate BB indicators
            self.strategy.prepare_dataframe(ohlcv)
            self.current_bb = self.strategy.get_latest_bb()

            # Fetch current price
            self.current_price = self.exchange.fetch_current_price()
            if self.current_price <= 0:
                logger.warning("Invalid current price fetched")
                return False

            return True

        except Exception as e:
            self.last_error = f"Data fetch error: {e}"
            logger.error(self.last_error)
            return False

    # ─── BALANCE FETCHING ───

    def fetch_balances(self) -> Dict[str, Any]:
        """Fetch account balances.

        When the SharkEx wallet returns INR-denominated balances (the account
        is INR-margined), this method converts INR → USDT using the configured
        ``USD_INR_RATE`` so that downstream code always works with USDT values.
        The original INR values are preserved under the ``"INR"`` key for the
        display.
        """
        try:
            raw = self.exchange.fetch_balance()

            # If the exchange returned an INR balance (INR-margined account),
            # compute the USDT equivalent and inject it under "USDT" so that
            # execute_entry() etc. can check ``balances.get("USDT")``.
            inr_info = raw.get("INR", {})
            inr_balance = inr_info.get("free", 0) if isinstance(inr_info, dict) else float(inr_info)
            if inr_balance > 0 and "USDT" not in raw:
                rate = self.cfg.risk.usd_inr_rate
                if rate > 0:
                    total_raw = float(raw.get("INR", {}).get("total", 0) if isinstance(raw.get("INR"), dict) else 0)
                    raw["USDT"] = {
                        "free": inr_balance / rate,
                        "total": total_raw / rate,
                    }

            return raw
        except Exception as e:
            logger.error(f"Balance fetch error: {e}")
            return {}

    # ─── ENTRY LOGIC ───

    def _get_bid_price(self) -> float:
        """Get best bid price from order book for limit entry."""
        try:
            # Use ticker best bid
            ticker = self.exchange.fetch_ticker()
            bid = float(ticker.get("bidPrice", 0) or ticker.get("lastPrice", 0))
            if bid > 0:
                return bid
        except Exception:
            pass
        return self.current_price

    def execute_entry(self, signal: SignalResult) -> bool:
        """
        Execute entry based on the squeeze breakout signal.

        Strategy:
        1. Place LIMIT order at current best bid.
        2. Wait up to ``limit_order_timeout`` seconds for fill.
        3. If not filled, cancel limit and place MARKET order.

        Returns True if entry was successful.
        """
        if self.position is not None:
            logger.debug("Already in a position, skipping entry")
            return False

        # Validate signal
        if signal.signal != "LONG":
            return False

        # Check risk limits
        if not self.risk_manager.can_enter_new_trade():
            logger.warning(f"Cannot enter: {self.risk_manager.lock_reason}")
            return False

        # Calculate position size
        trade_usdt = self.cfg.risk.trade_size_inr / self.cfg.risk.usd_inr_rate
        quantity = trade_usdt / signal.candle_close
        quantity = self._round_quantity(quantity)

        if quantity <= 0:
            logger.error(f"Invalid quantity calculated: {quantity}")
            return False

        # Check sufficient balance
        balances = self.fetch_balances()
        usdt_free = balances.get("USDT", {}).get("free", 0)
        if usdt_free < trade_usdt:
            logger.warning(f"Insufficient USDT balance: {usdt_free:.2f} < {trade_usdt:.2f}")
            return False

        # LONG → buy
        side = "buy"
        position_side = OrderSide.BUY

        # ── Step 1: Limit order at bid ──
        bid_price = self._get_bid_price()
        # Ensure limit price is ≤ current mid (bid should already be)
        if bid_price <= 0 or bid_price > self.current_price * 1.01:
            bid_price = round(self.current_price * 0.999, 4)

        logger.info(f"Placing LIMIT buy: {quantity:.6f} BTC @ ${bid_price:.4f} (bid)")
        limit_order = self.exchange.create_limit_order(side, quantity, bid_price)

        if limit_order is None:
            # Limit order failed outright — fallback to market
            logger.warning("Limit order creation failed, falling back to market")
            return self._execute_market_entry(side, position_side, quantity, trade_usdt, signal)

        fill_price = self._wait_for_fill(limit_order.id, bid_price, self.cfg.strategy.limit_order_timeout)

        if fill_price is not None:
            # Limit order filled
            return self._complete_entry(position_side, fill_price, quantity, trade_usdt, signal, "LIMIT")

        # ── Step 2: Cancel limit and market fallback ──
        logger.info("Limit order not filled in time, switching to MARKET")
        self.exchange.cancel_order(limit_order.id)
        return self._execute_market_entry(side, position_side, quantity, trade_usdt, signal)

    def _wait_for_fill(self, order_id: str, limit_price: float, timeout: int) -> Optional[float]:
        """Poll order status for up to ``timeout`` seconds.  Return fill price or None."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            order = self.exchange.fetch_order(order_id)
            if order is None:
                time.sleep(0.5)
                continue

            if order.status == OrderStatus.FILLED:
                avg = order.average_price if order.average_price > 0 else limit_price
                logger.info(f"Limit order filled @ ${avg:.4f}")
                return avg

            if order.status == OrderStatus.CANCELLED:
                logger.info("Limit order was cancelled externally")
                return None

            time.sleep(0.5)

        return None

    def _execute_market_entry(
        self, side: str, position_side: OrderSide,
        quantity: float, trade_usdt: float, signal: SignalResult,
    ) -> bool:
        """Fallback market order execution."""
        logger.info(f"Executing MARKET {side}: {quantity:.6f} BTC")
        order = self.exchange.create_market_order(side, quantity)
        if order is None:
            logger.error("Market entry order failed!")
            self.display.print_error("Market order execution failed")
            return False

        avg_price = order.average_price if order.average_price > 0 else self.current_price
        actual_filled = order.filled if order.filled > 0 else quantity
        return self._complete_entry(position_side, avg_price, actual_filled, trade_usdt, signal, "MARKET")

    def _complete_entry(
        self, position_side: OrderSide, entry_price: float,
        quantity: float, trade_usdt: float, signal: SignalResult, order_type: str,
    ) -> bool:
        """Finalise a filled entry: create Position, place stop, log."""
        # Create position object
        self.position = Position(
            symbol=self.cfg.exchange.symbol,
            side=position_side,
            entry_price=entry_price,
            quantity=quantity,
            usdt_invested=entry_price * quantity,
            inr_invested=(entry_price * quantity) * self.cfg.risk.usd_inr_rate,
            entry_time=time.time(),
            highest_price=entry_price,
            lowest_price=entry_price,
        )

        # Calculate initial trailing stop
        self.position.trailing_stop_price = self.strategy.calculate_initial_stop(self.position)

        # Place stop-loss order
        self._place_stop_order()

        self.display.print_trade_executed(
            signal.signal, entry_price, quantity,
            self.position.usdt_invested, self.position.inr_invested,
        )

        logger.info(
            "✅ %s ENTRY [%s] @ $%.4f | Qty: %.6f | Stop: $%.4f",
            signal.signal, order_type, entry_price, quantity,
            self.position.trailing_stop_price,
        )
        self._add_web_log(
            f"✅ ENTRY {signal.signal} [{order_type}] @ ${entry_price:.4f} | Qty: {quantity:.6f}",
            "log-entry",
        )
        self._trade_logger.info(
            f"ENTRY {signal.signal} [{order_type}] | Price: ${entry_price:.4f} | "
            f"Qty: {quantity:.6f} | Stop: ${self.position.trailing_stop_price:.4f}"
        )

        return True

    def _place_stop_order(self) -> bool:
        """Place the initial stop-loss order for the current position."""
        if self.position is None:
            return False

        # Determine stop side (opposite of position)
        if self.position.side == OrderSide.BUY:
            stop_side = "sell"
        else:
            stop_side = "buy"

        stop_price = self.position.trailing_stop_price

        # For LONG: use STOP_LOSS_LIMIT with limit_price slightly below stop
        # to prevent slippage on the stop trigger.
        if self.position.side == OrderSide.BUY:
            limit_price = round(stop_price * 0.999, 4)
            order = self.exchange.create_stop_order(
                side=stop_side,
                quantity=self.position.quantity,
                stop_price=stop_price,
            )
        else:
            order = self.exchange.create_stop_order(
                side=stop_side,
                quantity=self.position.quantity,
                stop_price=stop_price,
            )

        if order:
            self.position.stop_order_id = order.id
            logger.info(f"Stop order placed: ID={order.id}, Price=${stop_price:.4f}")
            return True
        else:
            logger.error("Failed to place stop order!")
            return False

    def _update_stop_order(self) -> bool:
        """
        Update the trailing stop by cancelling old stop order and placing new one.
        Called when trailing stop price has moved.
        """
        if self.position is None:
            return False

        # Cancel old stop order
        if self.position.stop_order_id:
            cancelled = self.exchange.cancel_order(self.position.stop_order_id)
            if cancelled:
                logger.debug(f"Old stop order {self.position.stop_order_id} cancelled")
            else:
                logger.warning(f"Failed to cancel old stop order {self.position.stop_order_id}")
                # Check if it was already filled
                old_order = self.exchange.fetch_order(self.position.stop_order_id)
                if old_order and old_order.status == OrderStatus.FILLED:
                    logger.info("Old stop order was already filled! Position should be closed.")
                    return False

        # Determine stop side
        if self.position.side == OrderSide.BUY:
            stop_side = "sell"
        else:
            stop_side = "buy"

        # Place new stop order
        order = self.exchange.create_stop_order(
            side=stop_side,
            quantity=self.position.quantity,
            stop_price=self.position.trailing_stop_price,
        )

        if order:
            self.position.stop_order_id = order.id
            logger.info(f"Stop order updated: ID={order.id}, Price=${self.position.trailing_stop_price:.4f}")
            return True
        else:
            logger.error("Failed to place new stop order!")
            return False

    # ─── EXIT LOGIC ───

    def check_and_execute_exit(self) -> bool:
        """
        Check exit conditions and execute if triggered.

        Only uses trailing stop (no take-profit).
        Returns True if position was closed.
        """
        if self.position is None:
            return False

        # Update trailing stop
        old_stop = self.position.trailing_stop_price
        new_stop = self.strategy.update_trailing_stop(self.position, self.current_price)

        if new_stop != old_stop:
            self.position.trailing_stop_price = new_stop
            # Trailing stop moved - update the stop order
            if not self._update_stop_order():
                # Stop order update failed - might mean it was filled
                if self._check_stop_filled():
                    self._close_position("SL")
                    return True

        # Check if stop loss is hit
        if self.strategy.check_stop_loss(self.position, self.current_price):
            self._close_position("SL")
            return True

        return False

    def _check_stop_filled(self) -> bool:
        """Check if the stop order was filled by querying the exchange."""
        if self.position and self.position.stop_order_id:
            order = self.exchange.fetch_order(self.position.stop_order_id)
            if order and order.status == OrderStatus.FILLED:
                return True
        return False

    def _close_position(self, reason: str):
        """
        Close the current position on the exchange.

        Args:
            reason: "SL" (stop-loss) or "MANUAL" (emergency/user)
        """
        if self.position is None:
            return

        # Cancel any existing stop order
        if self.position.stop_order_id:
            self.exchange.cancel_order(self.position.stop_order_id)

        # Determine exit side (opposite of position)
        if self.position.side == OrderSide.BUY:
            exit_side = "sell"
        else:
            exit_side = "buy"

        # Execute market order to close
        order = self.exchange.create_market_order(exit_side, self.position.quantity)

        if order:
            exit_price = order.average_price if order.average_price > 0 else self.current_price
            actual_filled = order.filled if order.filled > 0 else self.position.quantity

            # Calculate P&L
            if self.position.side == OrderSide.BUY:
                pnl_usdt = (exit_price - self.position.entry_price) * actual_filled
            else:
                pnl_usdt = (self.position.entry_price - exit_price) * actual_filled

            # Record trade
            self.risk_manager.record_trade(
                side="LONG" if self.position.side == OrderSide.BUY else "SHORT",
                entry_price=self.position.entry_price,
                exit_price=exit_price,
                quantity=actual_filled,
                exit_reason=reason,
                entry_time=self.position.entry_time,
                exit_time=time.time(),
            )

            self.display.print_trade_exit(
                "LONG" if self.position.side == OrderSide.BUY else "SHORT",
                exit_price,
                pnl_usdt,
                pnl_usdt * self.cfg.risk.usd_inr_rate,
                reason,
            )

            pnl_inr = pnl_usdt * self.cfg.risk.usd_inr_rate
            pnl_cls = "log-profit" if pnl_usdt >= 0 else "log-loss"
            logger.info(f"✅ EXIT [{reason}] @ ${exit_price:.4f} | P&L: ${pnl_usdt:+.2f} (₹{pnl_inr:+.2f})")
            self._add_web_log(f"✅ EXIT [{reason}] P&L: ${pnl_usdt:+.2f}", pnl_cls)
            self._trade_logger.info(f"EXIT {reason} | Price: ${exit_price:.4f} | P&L: ${pnl_usdt:+.4f} | ₹{pnl_inr:+.2f}")
        else:
            logger.error("Failed to close position! Manual intervention may be required.")
            self.display.print_error("Failed to close position - check exchange!")
            self._add_web_log("❌ Failed to close position!", "log-error")

        self.position = None

    def close_all_positions(self):
        """Emergency close all positions."""
        if self.position:
            logger.warning("⚠️  EMERGENCY CLOSE: Closing all positions now!")
            self._close_position("MANUAL")

    # ─── INTERACTIVE ACTIONS ───

    def _process_actions(self):
        """
        Process any pending actions from the interactive CLI display.

        Actions:
          - "pause": Halt trading decisions (keep fetching data and updating display)
          - "resume": Resume trading decisions
          - "update_config": Apply a config change and rebuild dependent objects
          - "reset_daily": Reset the risk manager's daily counters
          - "shutdown": Graceful shutdown
        """
        for action_type, payload in self.display.pending_actions:
            if action_type == "pause":
                if not self.paused:
                    self.paused = True
                    logger.info("PAUSED TRADING by user")
                    self.display.paused = True

            elif action_type == "resume":
                if self.paused:
                    self.paused = False
                    logger.info("RESUMED TRADING by user")
                    self.display.paused = False

            elif action_type == "update_config":
                config_path = payload.get("path", "")
                label = payload.get("label", "")
                value = payload.get("value", "")
                logger.info(f"Config updated by user: {label} -> {value} (path={config_path})")

                # Rebuild strategy if strategy params changed
                if config_path.startswith("strategy."):
                    self.strategy = BollingerBandStrategy(self.cfg.strategy)
                    logger.info(f"   Strategy rebuilt with new {label}")

                # Rebuild risk manager if risk params changed
                if config_path.startswith("risk."):
                    old_lock = self.risk_manager._is_locked
                    old_daily_pnl = self.risk_manager._daily_pnl_usdt
                    old_trades = self.risk_manager._trades_today
                    old_trade_log = self.risk_manager._trade_log
                    old_counter = self.risk_manager._trade_counter
                    self.risk_manager = RiskManager(self.cfg.risk)
                    self.risk_manager._is_locked = old_lock
                    self.risk_manager._daily_pnl_usdt = old_daily_pnl
                    self.risk_manager._trades_today = old_trades
                    self.risk_manager._trade_log = old_trade_log
                    self.risk_manager._trade_counter = old_counter
                    self.risk_manager._current_date = datetime.now(IST).strftime("%Y-%m-%d")
                    logger.info(f"   Risk manager rebuilt with new {label}")

                # Handle exchange config changes
                if config_path.startswith("exchange."):
                    if config_path == "exchange.leverage":
                        try:
                            lev = int(value)
                            contract = self.cfg.exchange.contract_name
                            success = self.exchange.set_leverage(lev, contract)
                            if success:
                                logger.info(f"   Leverage updated to {lev}x for {contract}")
                            else:
                                logger.error(f"   Failed to update leverage to {lev}x")
                        except Exception as e:
                            logger.error(f"   Leverage update exception: {e}")
                    elif config_path == "exchange.symbol":
                        old_symbol = self.cfg.exchange.symbol
                        self.cfg.exchange.symbol = value
                        try:
                            self.exchange = create_exchange_client(self.cfg)
                            logger.info(f"   Exchange client rebuilt with symbol {value} (was {old_symbol})")
                        except Exception as e:
                            logger.error(f"   Failed to rebuild exchange client: {e}")
                            self.cfg.exchange.symbol = old_symbol

            elif action_type == "reset_daily":
                self.risk_manager._current_date = ""
                self.risk_manager.check_and_reset_daily()
                logger.info("Daily stats RESET by user")

            elif action_type == "shutdown":
                logger.info("Shutdown requested from interactive display")
                self.running = False

    def _handle_web_config(self, key: str, value):
        """Apply a config change received from the web dashboard."""
        try:
            if key == "toggle_pause":
                self.paused = not self.paused
                self.display.paused = self.paused
                logger.info(f"{'PAUSED' if self.paused else 'RESUMED'} trading via web dashboard")
                return

            if key == "trade_size_inr":
                self.cfg.risk.trade_size_inr = float(value)
                logger.info(f"[Web] trade_size_inr = {value}")
            elif key == "leverage":
                lev = int(value)
                self.cfg.exchange.leverage = lev
                try:
                    self.exchange.set_leverage(lev, self.cfg.exchange.contract_name)
                    logger.info(f"[Web] Leverage updated to {lev}x")
                except Exception as e:
                    logger.error(f"[Web] Leverage update failed: {e}")
            elif key == "symbol":
                old = self.cfg.exchange.symbol
                self.cfg.exchange.symbol = value
                try:
                    self.exchange = create_exchange_client(self.cfg)
                    logger.info(f"[Web] Symbol changed: {old} -> {value}")
                except Exception as e:
                    logger.error(f"[Web] Symbol change failed: {e}")
            elif key == "contract_name":
                self.cfg.exchange.contract_name = value
                logger.info(f"[Web] contract_name = {value}")
            elif key == "bb_period":
                self.cfg.strategy.bb_period = int(value)
                self.strategy = BollingerBandStrategy(self.cfg.strategy)
                logger.info(f"[Web] bb_period = {value}")
            elif key == "bb_stddev":
                self.cfg.strategy.bb_stddev = float(value)
                self.strategy = BollingerBandStrategy(self.cfg.strategy)
                logger.info(f"[Web] bb_stddev = {value}")
            elif key == "squeeze_lookback":
                self.cfg.strategy.squeeze_lookback = int(value)
                self.strategy = BollingerBandStrategy(self.cfg.strategy)
                logger.info(f"[Web] squeeze_lookback = {value}")
            elif key == "breakout_lookback":
                self.cfg.strategy.breakout_lookback = int(value)
                self.strategy = BollingerBandStrategy(self.cfg.strategy)
                logger.info(f"[Web] breakout_lookback = {value}")
            elif key == "trailing_lookback":
                self.cfg.strategy.trailing_lookback = int(value)
                self.strategy = BollingerBandStrategy(self.cfg.strategy)
                logger.info(f"[Web] trailing_lookback = {value}")
            elif key == "limit_order_timeout":
                self.cfg.strategy.limit_order_timeout = int(value)
                logger.info(f"[Web] limit_order_timeout = {value}s")
            elif key == "short_enabled":
                self.cfg.strategy.short_enabled = value if isinstance(value, bool) else str(value).lower() in ("1", "true", "yes")
                self.strategy = BollingerBandStrategy(self.cfg.strategy)
                logger.info(f"[Web] short_enabled = {self.cfg.strategy.short_enabled}")
            elif key == "poll_interval":
                self.cfg.poll_interval_sec = float(value)
                logger.info(f"[Web] poll_interval = {value}s")
        except Exception as e:
            logger.error(f"[Web] Config update error ({key}={value}): {e}")

    def _push_web_state(self):
        """Serialize current bot state and push to the web API server."""
        try:
            balances = self.fetch_balances()
            usdt_balance = 0.0
            for asset, val in balances.items():
                if asset == "USDT":
                    usdt_balance = float(val.get("free", val)) if isinstance(val, dict) else float(val)

            price_change_pct = 0.0
            high_24h = 0.0
            low_24h = 0.0
            volume_24h = 0.0
            try:
                ticker = self.exchange.fetch_ticker()
                price_change_pct = float(ticker.get("priceChangePercent", 0))
                high_24h = float(ticker.get("highPrice", 0))
                low_24h = float(ticker.get("lowPrice", 0))
                volume_24h = float(ticker.get("volume", 0))
            except Exception:
                pass

            signal = self.strategy.detect_entry_signal()

            # ── BB data ──
            bb_data = None
            if self.current_bb:
                bb_data = {
                    "upper": self.current_bb.upper,
                    "sma": self.current_bb.sma,
                    "lower": self.current_bb.lower,
                    "width": self.current_bb.width,
                    "volatility": self.current_bb.volatility,
                }

            # ── Position data ──
            pos_data = None
            if self.position:
                pos_data = {
                    "side": str(self.position.side),
                    "entry_price": self.position.entry_price,
                    "mark_price": self.position.mark_price,
                    "quantity": self.position.quantity,
                    "usdt_invested": self.position.usdt_invested,
                    "inr_invested": self.position.inr_invested,
                    "unrealized_pnl": self.position.unrealized_pnl,
                    "unrealized_pnl_pct": self.position.unrealized_pnl_pct,
                    "trailing_stop_price": self.position.trailing_stop_price,
                    "highest_price": self.position.highest_price,
                    "lowest_price": self.position.lowest_price,
                }

            # ── Recent trades ──
            recent = []
            for t in self.risk_manager.get_recent_trades(20):
                recent.append({
                    "id": t.id,
                    "side": t.side,
                    "entry_price": t.entry_price,
                    "exit_price": t.exit_price,
                    "quantity": t.quantity,
                    "pnl_usdt": t.pnl_usdt,
                    "pnl_inr": t.pnl_inr,
                    "exit_reason": t.exit_reason,
                    "exit_time": t.exit_time,
                })

            # ── Daily stats ──
            all_trades_today = self.risk_manager.get_recent_trades(100)
            winning = sum(1 for t in all_trades_today if t.pnl_usdt > 0)
            total_t = self.risk_manager.trades_today
            daily = {
                "date": self.risk_manager._current_date if hasattr(self.risk_manager, "_current_date") else datetime.now(IST).strftime("%Y-%m-%d"),
                "total_trades": total_t,
                "winning_trades": winning,
                "losing_trades": total_t - winning,
                "total_pnl_usdt": self.risk_manager.daily_pnl_usdt,
                "total_pnl_inr": self.risk_manager.daily_pnl_inr,
                "is_locked": self.risk_manager._is_locked if hasattr(self.risk_manager, "_is_locked") else False,
            }

            # ── Assemble state ──
            state = {
                "current_price": self.current_price,
                "price_change_pct": price_change_pct,
                "high_24h": high_24h,
                "low_24h": low_24h,
                "volume_24h": volume_24h,
                "usdt_balance": usdt_balance,
                "inr_value": usdt_balance * self.cfg.risk.usd_inr_rate,
                "trade_size_inr": self.cfg.risk.trade_size_inr,
                "max_daily_loss_inr": self.cfg.risk.max_daily_loss_inr,
                "usd_inr_rate": self.cfg.risk.usd_inr_rate,
                "symbol": self.cfg.exchange.symbol,
                "leverage": self.cfg.exchange.leverage,
                "cycle_count": self.cycle_count,
                "paused": self.paused,
                "daily_locked": self.risk_manager._is_locked if hasattr(self.risk_manager, "_is_locked") else False,
                "position": pos_data,
                "bb_result": bb_data,
                "signal": signal.signal,
                "signal_reason": signal.reason,
                "squeeze_active": signal.squeeze_active,
                "squeeze_width": signal.squeeze_width,
                "squeeze_min": signal.squeeze_min,
                "highest_10": signal.highest_10,
                "lowest_5": signal.lowest_5,
                "candle_close": signal.candle_close,
                "candle_time": str(signal.candle_time),
                "bb_period": self.cfg.strategy.bb_period,
                "bb_stddev": self.cfg.strategy.bb_stddev,
                "candle_tf": self.cfg.strategy.candle_tf,
                "squeeze_lookback": self.cfg.strategy.squeeze_lookback,
                "breakout_lookback": self.cfg.strategy.breakout_lookback,
                "trailing_lookback": self.cfg.strategy.trailing_lookback,
                "limit_order_timeout": self.cfg.strategy.limit_order_timeout,
                "poll_interval": self.cfg.poll_interval_sec,
                "short_enabled": self.cfg.strategy.short_enabled,
                "in_session": self._is_in_trading_session(),
                "session_hours": [
                    f"{sh:02d}:{sm:02d}-{eh:02d}:{em:02d}"
                    for sh, sm, eh, em in SESSION_HOURS
                ],
                "daily_stats": daily,
                "recent_trades": recent,
                "last_error": self.last_error,
                "contract_name": self.cfg.exchange.contract_name,
                "max_trades_per_day": self.cfg.risk.max_trades_per_day,
                "logs": list(self.web_logs[-50:]),  # Last 50 log entries for web
            }

            web_api.update_state(state)
        except Exception:
            logger.debug(f"Web state push failed: {traceback.format_exc()}")

    # ─── MAIN CYCLE ───

    def run_cycle(self):
        """
        Execute one complete cycle of the bot:
        1. Fetch data
        2. Check exit conditions (unless paused)
        3. Detect and execute entry signals (unless paused, only during trading sessions)
        4. Update display
        """
        self.cycle_count += 1

        # 1. Daily reset check
        self.risk_manager.check_and_reset_daily()

        # 2. Fetch data (always, even when paused — keep display alive)
        if not self.fetch_data():
            self.display.update_data(
                last_error=self.last_error,
                bot_status="PAUSED (Data)" if self.paused else "ERROR (Data)",
            )
            self._push_web_state()  # Still push state so dashboard shows status
            return

        # 3. TRADING LOGIC — skip if paused
        if not self.paused:
            # Auto-close position if daily limits are breached
            if self.position and self.risk_manager.is_locked:
                self._close_position("LIMIT_BREACH")
                logger.warning(f"🔒 Position closed: {self.risk_manager.lock_reason}")

            # Check exit conditions (always, even outside sessions)
            if self.position:
                self.check_and_execute_exit()

            # Entry logic — only during trading sessions
            in_session = self._is_in_trading_session()
            if self.position is None and in_session:
                signal = self.strategy.detect_entry_signal()
                if signal.signal != "NONE":
                    self.display.print_signal_detected(signal.signal, signal.reason)
                    self.execute_entry(signal)
                elif self.cycle_count % 5 == 1:  # log every 5 cycles in session
                    logger.info(f"[Cycle {self.cycle_count}] No signal: {signal.reason}")
            elif self.position is None and not in_session:
                if self.cycle_count % 60 == 1:
                    now = datetime.now(IST)
                    logger.info(f"[Cycle {self.cycle_count}] Outside trading session ({now.strftime('%H:%M IST')}) — waiting for next session window")

        # 4. Update display data
        self._update_display()

    def _update_display(self):
        """Prepare data and push to the CLI display."""
        # Always push web state FIRST (has its own error handling)
        try:
            self._push_web_state()
        except Exception:
            logger.debug(f"Web state push failed in _update_display: {traceback.format_exc()}")

        try:
            balances = self.fetch_balances()

            # Flatten nested balances for the display table.
            display_balances: Dict[str, float] = {}
            for asset, val in balances.items():
                if isinstance(val, dict):
                    display_balances[asset] = float(val.get("free", 0))
                elif isinstance(val, (int, float)):
                    display_balances[asset] = float(val)

            # Get recent trades
            recent_trades = self.risk_manager.get_recent_trades(20)

            # Get latest signal info
            signal = self.strategy.detect_entry_signal()

            self.display.update_data(
                current_price=self.current_price,
                balance=display_balances,
                position=self.position,
                bb_result=self.current_bb,
                signal=signal.signal,
                signal_reason=signal.reason,
                last_signal_distance=0.0,
                nearest_trade_direction="",
                nearest_trade_distance_pct=0.0,
                nearest_trade_trigger_price=0.0,
                bot_status="RUNNING" if self.running else "STOPPING",
                cycle_count=self.cycle_count,
                last_error=self.last_error,
                paper_trading=False,
                risk_manager=self.risk_manager,
                recent_trades=recent_trades,
            )
        except Exception:
            logger.debug(f"Display update failed: {traceback.format_exc()}")

    # ─── RUN LOOP ───

    def run(self):
        """Main bot loop."""
        # ── Build startup banner (also print raw so it's visible even if TUI hides logs) ──
        banner_lines = [
            "=" * 60,
            "🚀 BB SQUEEZE BREAKOUT BOT STARTING (LIVE)",
            "=" * 60,
            f"Exchange: SharkEx v1",
            f"Symbol: {self.cfg.exchange.symbol}",
            f"Timeframe: {self.cfg.strategy.candle_tf}",
            f"Trade Size: ₹{self.cfg.risk.trade_size_inr:,.0f}",
            f"Max Daily Loss: ₹{self.cfg.risk.max_daily_loss_inr:,.0f}",
            f"Max Trades/Day: {self.cfg.risk.max_trades_per_day}",
            f"Poll Interval: {self.cfg.poll_interval_sec}s",
            f"BB: ({self.cfg.strategy.bb_period},{self.cfg.strategy.bb_stddev}) | "
            f"Squeeze: {self.cfg.strategy.squeeze_lookback} | "
            f"Breakout: {self.cfg.strategy.breakout_lookback} | "
            f"Trail: {self.cfg.strategy.trailing_lookback}",
            f"Limit Order Timeout: {self.cfg.strategy.limit_order_timeout}s",
            f"SHORT Signals: {'ON' if self.cfg.strategy.short_enabled else 'OFF (LONG only)'}",
            f"Data Window: {self.cfg.strategy.data_window} candles",
            f"Sessions (IST): " + ", ".join(
                f"{sh:02d}:{sm:02d}-{eh:02d}:{em:02d}" for sh, sm, eh, em in SESSION_HOURS
            ),
            "=" * 60,
        ]
        for line in banner_lines:
            logger.info(line)
            sys.stderr.write(line + "\n")
        sys.stderr.flush()

        # ═══ Start web API server FIRST (before TUI takes over the terminal) ═══
        web_started = web_api.start_server(host="0.0.0.0", port=8080)
        if web_started:
            msg = "🌐 Web dashboard: http://0.0.0.0:8080"
            logger.info(msg)
            sys.stderr.write(msg + "\n")
        else:
            msg = "⚠️  WARNING: Web server failed to start on port 8080!"
            logger.warning(msg)
            sys.stderr.write(msg + "\n")
        sys.stderr.flush()

        # Start live display (Rich TUI — once this starts it takes over stdout/stderr)
        try:
            self.display.start_live_display()
        except Exception as e:
            logger.warning(f"Live display start failed: {e}. Running in log-only mode.")

        logger.info("Bot loop started. Press Ctrl+C to stop.")
        sys.stderr.write("▶ Bot loop started. Press Ctrl+C to stop.\n")
        sys.stderr.flush()

        while self.running:
            try:
                cycle_start = time.time()

                # Process any pending interactive actions
                self._process_actions()

                # Run one cycle (skip trading logic if paused)
                self.run_cycle()

                # Process keystrokes + let auto_refresh handle rendering
                try:
                    self.display.refresh()
                except Exception:
                    pass  # Display errors shouldn't stop the bot

                # Sleep between cycles
                base_interval = 1.0 if self.paused else self.cfg.poll_interval_sec
                sleep_time = max(0.2, base_interval - (time.time() - cycle_start))

                # Sleep in 0.5s chunks so we can process keystrokes and shutdown quickly
                while sleep_time > 0 and self.running:
                    chunk = min(0.5, sleep_time)
                    time.sleep(chunk)
                    sleep_time -= chunk
                    self._process_actions()
                    # Only process keystrokes during sleep, let auto_refresh render
                    try:
                        self.display.tick()
                    except Exception:
                        pass

            except KeyboardInterrupt:
                logger.info("Keyboard interrupt received")
                break
            except Exception as e:
                self.last_error = traceback.format_exc()
                logger.error(f"Cycle error: {e}\n{traceback.format_exc()}")
                self.display.print_error(str(e))
                time.sleep(5)  # Wait before retrying

        # Cleanup
        self.shutdown()

    def shutdown(self):
        """Graceful shutdown."""
        logger.info("Shutting down...")

        # Close any open position
        if self.position:
            logger.warning("Closing open position during shutdown...")
            self._close_position("MANUAL")

        # Stop display
        try:
            self.display.stop_live_display()
        except Exception:
            pass

        # Stop web API server
        try:
            web_api.stop_server()
        except Exception:
            pass

        # Print final stats
        logger.info("=" * 60)
        logger.info("📊 FINAL SESSION STATS")
        logger.info(f"Total Cycles: {self.cycle_count}")
        logger.info(f"Daily P&L: ₹{self.risk_manager.daily_pnl_inr:,.2f}")
        logger.info(f"Trades Today: {self.risk_manager.trades_today}")
        logger.info("=" * 60)
        logger.info("Bot stopped. Goodbye! 👋")

    def print_cli_header(self):
        """Print a one-time header for logging mode."""
        print("\n" + "=" * 70)
        print("   🤖 BB SQUEEZE BREAKOUT BOT — LIVE")
        print("=" * 70)
        print(f"   Exchange: SharkEx v1 | "
              f"Symbol: {self.cfg.exchange.symbol} | "
              f"TF: {self.cfg.strategy.candle_tf}")
        print(f"   Trade Size: ₹{self.cfg.risk.trade_size_inr:,.0f} | "
              f"Max Loss: ₹{self.cfg.risk.max_daily_loss_inr:,.0f} | "
              f"Max Trades: {self.cfg.risk.max_trades_per_day}")
        print("=" * 70 + "\n")


# =============================================================================
# Entry Point
# =============================================================================

def main():
    """Entry point for the bot."""
    import argparse

    parser = argparse.ArgumentParser(
        description="15-Minute BB Squeeze Breakout Bot for SharkEx (LIVE trading)"
    )
    parser.add_argument(
        "--config", type=str, default=None,
        help="Path to a .env config file"
    )
    parser.add_argument(
        "--no-interactive", action="store_true",
        help="Skip interactive setup, use .env or defaults"
    )
    parser.add_argument(
        "--daemon", action="store_true",
        help="Run as a background daemon process (Unix only)"
    )
    args = parser.parse_args()

    # Daemonize before any heavy init (Unix only)
    if args.daemon:
        if sys.platform == "win32":
            sys.stderr.write("--daemon is not supported on Windows\n")
            sys.exit(1)
        daemonize()

    # Setup file-based logging (always on in production)
    setup_file_logging()

    # Load .env file into os.environ before reading config.
    from dotenv import load_dotenv, find_dotenv

    env_path = args.config or find_dotenv()
    if env_path:
        loaded = load_dotenv(env_path)
    else:
        loaded = load_dotenv(os.path.join(
            os.path.dirname(os.path.abspath(__file__)), ".env"
        ))

    # Temporary debug — remove once confirmed working
    import sys as _sys
    _sys.stderr.write(
        f"[DEBUG] .env path tried: {env_path or os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')}\n"
    )
    _sys.stderr.write(
        f"[DEBUG] load_dotenv returned: {loaded}\n"
    )
    _sys.stderr.write(
        f"[DEBUG] SHARKEX_API_KEY = {'SET' if os.getenv('SHARKEX_API_KEY') else 'EMPTY'}\n"
    )

    # Load configuration
    if args.no_interactive:
        cfg = BotConfig.from_env()
    else:
        cfg = BotConfig.interactive_setup()

    # Live trading only — no paper mode
    if not cfg.exchange.api_key or not cfg.exchange.api_secret:
        print("\n⚠️  ERROR: API key and secret are required for live trading!")
        print("   Set SHARKEX_API_KEY and SHARKEX_API_SECRET in .env")
        sys.exit(1)

    # Create bot
    bot = TradingBot(cfg)

    try:
        bot.run()
    except KeyboardInterrupt:
        bot.shutdown()
    except Exception as e:
        logger.critical(f"Fatal error: {e}\n{traceback.format_exc()}")
        bot.shutdown()
        sys.exit(1)


if __name__ == "__main__":
    main()