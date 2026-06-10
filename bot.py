"""
bot.py - Bollinger Band Reversal Trading Bot (Main Engine, 24/7)

Orchestrates all modules:
- SharkEx v1 exchange connection & data fetching
- Strategy signal detection (Bollinger Band reversal)
- Order execution & trailing stop management
- Risk management enforcement
- Live CLI display

Usage:
    python bot.py          # Interactive setup (asks for API keys)
    python bot.py --paper  # Paper trading mode (no real orders)
    python bot.py --no-interactive  # Use .env or defaults, no prompts
"""

import os
import sys
import time
import signal
import logging
import traceback
from datetime import datetime
from typing import Optional, Dict, Any

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

# ─── Logging Setup ───
logger = logging.getLogger("bot")

IST = pytz.timezone("Asia/Kolkata")


# =============================================================================
# Main Trading Bot
# =============================================================================

class TradingBot:
    """
    Main bot class that orchestrates the entire trading system.
    Runs 24/7 — no session time restrictions.
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

        # Paper trading
        self.paper_balance: Dict[str, float] = {"USDT": 10000.0, "BTC": 0.0}
        self.paper_entry_price: float = 0.0
        self.paper_quantity: float = 0.0

        # Register signal handlers for graceful shutdown
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

    def _signal_handler(self, signum, frame):
        """Handle graceful shutdown."""
        logger.info(f"\nSignal {signum} received. Shutting down gracefully...")
        self.running = False

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

            # Prepare DataFrame and calculate BB
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
        """Fetch account balances."""
        try:
            if self.cfg.paper_trading:
                return {
                    "USDT": {"free": self.paper_balance["USDT"], "total": self.paper_balance["USDT"]},
                    "BTC": {"free": self.paper_balance["BTC"], "total": self.paper_balance["BTC"]},
                }
            return self.exchange.fetch_balance()
        except Exception as e:
            logger.error(f"Balance fetch error: {e}")
            return {}

    # ─── ENTRY LOGIC ───

    def execute_entry(self, signal: SignalResult) -> bool:
        """
        Execute a market entry based on the signal.
        
        Returns True if entry was successful.
        """
        if self.position is not None:
            logger.debug("Already in a position, skipping entry")
            return False

        # Validate signal
        if signal.signal == "NONE":
            return False

        # Check risk limits
        if not self.risk_manager.can_enter_new_trade():
            logger.warning(f"Cannot enter: {self.risk_manager.lock_reason}")
            return False

        # Calculate position size
        trade_usdt = self.cfg.risk.trade_size_inr / self.cfg.risk.usd_inr_rate
        quantity = trade_usdt / signal.candle_close

        # Round quantity to appropriate precision
        quantity = self._round_quantity(quantity)

        if quantity <= 0:
            logger.error(f"Invalid quantity calculated: {quantity}")
            return False

        # Check sufficient balance
        balances = self.fetch_balances()
        if self.cfg.paper_trading:
            if self.paper_balance["USDT"] < trade_usdt:
                logger.warning(f"Insufficient paper USDT: {self.paper_balance['USDT']:.2f} < {trade_usdt:.2f}")
                return False
        else:
            usdt_free = balances.get("USDT", {}).get("free", 0)
            if usdt_free < trade_usdt:
                logger.warning(f"Insufficient USDT balance: {usdt_free:.2f} < {trade_usdt:.2f}")
                return False

        # Determine order side
        if signal.signal == "LONG":
            side = "buy"
            position_side = OrderSide.BUY
        else:
            side = "sell"
            position_side = OrderSide.SELL

        if self.cfg.paper_trading:
            # Paper trading execution
            entry_price = signal.candle_close
            self.paper_balance["USDT"] -= trade_usdt
            self.paper_balance["BTC"] += quantity
            self.paper_entry_price = entry_price
            self.paper_quantity = quantity

            # Create position
            self.position = Position(
                symbol=self.cfg.exchange.symbol,
                side=position_side,
                entry_price=entry_price,
                quantity=quantity,
                usdt_invested=trade_usdt,
                inr_invested=self.cfg.risk.trade_size_inr,
                entry_time=time.time(),
                highest_price=entry_price,
                lowest_price=entry_price,
                trailing_stop_price=self.strategy.calculate_initial_stop(
                    Position("", position_side, entry_price, quantity, trade_usdt, 0, 0)
                ),
            )

            self.display.print_trade_executed(
                signal.signal, entry_price, quantity, trade_usdt, self.cfg.risk.trade_size_inr
            )
            logger.info(f"📝 PAPER {signal.signal} @ ${entry_price:.2f} | Qty: {quantity:.6f} BTC")
            return True
        else:
            # Real exchange execution
            order = self.exchange.create_market_order(side, quantity)
            if order is None:
                logger.error("Market order failed!")
                self.display.print_error("Market order execution failed")
                return False

            avg_price = order.average_price if order.average_price > 0 else self.current_price
            actual_filled = order.filled if order.filled > 0 else quantity

            # Create position object
            self.position = Position(
                symbol=self.cfg.exchange.symbol,
                side=position_side,
                entry_price=avg_price,
                quantity=actual_filled,
                usdt_invested=avg_price * actual_filled,
                inr_invested=(avg_price * actual_filled) * self.cfg.risk.usd_inr_rate,
                entry_time=time.time(),
                highest_price=avg_price,
                lowest_price=avg_price,
            )

            # Calculate initial trailing stop
            self.position.trailing_stop_price = self.strategy.calculate_initial_stop(self.position)

            # Place stop-loss order
            self._place_stop_order()

            self.display.print_trade_executed(
                signal.signal, avg_price, actual_filled,
                self.position.usdt_invested, self.position.inr_invested
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

        if self.cfg.paper_trading:
            logger.info(f"📝 PAPER Stop placed at ${stop_price:.2f}")
            return True

        order = self.exchange.create_stop_order(
            side=stop_side,
            quantity=self.position.quantity,
            stop_price=stop_price,
        )

        if order:
            self.position.stop_order_id = order.id
            logger.info(f"Stop order placed: ID={order.id}, Price=${stop_price:.2f}")
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

        if self.cfg.paper_trading:
            return True

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
            logger.info(f"Stop order updated: ID={order.id}, Price=${self.position.trailing_stop_price:.2f}")
            return True
        else:
            logger.error("Failed to place new stop order!")
            return False

    # ─── EXIT LOGIC ───

    def check_and_execute_exit(self) -> bool:
        """
        Check exit conditions and execute if triggered.
        
        Returns True if position was closed.
        """
        if self.position is None or self.current_bb is None:
            return False

        # 1. Check take-profit
        if self.strategy.check_take_profit(self.position, self.current_price, self.current_bb):
            self._close_position("TP")
            return True

        # 2. Update trailing stop
        old_stop = self.position.trailing_stop_price
        new_stop = self.strategy.update_trailing_stop(self.position, self.current_price)

        if new_stop != old_stop:
            # Trailing stop moved - update the stop order
            if not self._update_stop_order():
                # Stop order update failed - might mean it was filled
                # Check if position was closed by stop
                if self._check_stop_filled():
                    self._close_position("SL")
                    return True

        # 3. Check if stop loss is hit
        if self.strategy.check_stop_loss(self.position, self.current_price):
            self._close_position("SL")
            return True

        return False

    def _check_stop_filled(self) -> bool:
        """Check if the stop order was filled by querying the exchange."""
        if self.cfg.paper_trading:
            return False

        if self.position and self.position.stop_order_id:
            order = self.exchange.fetch_order(self.position.stop_order_id)
            if order and order.status == OrderStatus.FILLED:
                return True
        return False

    def _close_position(self, reason: str):
        """
        Close the current position.
        
        Args:
            reason: "TP", "SL", or "MANUAL"
        """
        if self.position is None:
            return

        if self.cfg.paper_trading:
            # Paper trading exit
            if self.position.side == OrderSide.BUY:
                pnl_usdt = (self.current_price - self.paper_entry_price) * self.paper_quantity
                self.paper_balance["BTC"] -= self.paper_quantity
                self.paper_balance["USDT"] += self.paper_entry_price * self.paper_quantity + pnl_usdt
            else:
                pnl_usdt = (self.paper_entry_price - self.current_price) * self.paper_quantity
                self.paper_balance["BTC"] += self.paper_quantity
                self.paper_balance["USDT"] += self.paper_entry_price * self.paper_quantity + pnl_usdt

            exit_price = self.current_price
            quantity = self.paper_quantity

            self.risk_manager.record_trade(
                side="LONG" if self.position.side == OrderSide.BUY else "SHORT",
                entry_price=self.paper_entry_price,
                exit_price=exit_price,
                quantity=quantity,
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

            self.position = None
            self.paper_entry_price = 0.0
            self.paper_quantity = 0.0
            return

        # Real exchange exit
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
        else:
            logger.error("Failed to close position! Manual intervention may be required.")
            self.display.print_error("Failed to close position - check exchange!")

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

                # If strategy trail_pct changed, update existing position's trailing stop
                if config_path == "strategy.trail_pct" and self.position:
                    if self.position.side == "LONG":
                        self.position.trailing_stop = (
                            self.position.highest_price * (1 - self.cfg.strategy.trail_pct)
                        )
                    else:
                        self.position.trailing_stop = (
                            self.position.lowest_price * (1 + self.cfg.strategy.trail_pct)
                        )
                    logger.info(f"   Position trailing stop recalculated: ${self.position.trailing_stop:,.2f}")

            elif action_type == "reset_daily":
                self.risk_manager._current_date = ""
                self.risk_manager.check_and_reset_daily()
                logger.info("Daily stats RESET by user")

            elif action_type == "shutdown":
                logger.info("Shutdown requested from interactive display")
                self.running = False

    # ─── MAIN CYCLE ───

    def run_cycle(self):
        """
        Execute one complete cycle of the bot (24/7):
        1. Fetch data
        2. Check exit conditions (unless paused)
        3. Detect and execute entry signals (unless paused)
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
            return

        # 3. TRADING LOGIC — skip if paused
        if not self.paused:
            # Check exit conditions (always)
            if self.position:
                self.check_and_execute_exit()

            # Entry logic (24/7 — no session restrictions)
            if self.position is None:
                signal = self.strategy.detect_entry_signal()
                if signal.signal != "NONE":
                    self.display.print_signal_detected(signal.signal, signal.reason)
                    self.execute_entry(signal)

        # 4. Update display data
        self._update_display()

    def _update_display(self):
        """Prepare data and push to the CLI display."""
        balances = self.fetch_balances()

        # Get recent trades
        recent_trades = self.risk_manager.get_recent_trades(20)

        # Get latest signal info
        signal = self.strategy.detect_entry_signal()

        self.display.update_data(
            current_price=self.current_price,
            balance=balances,
            position=self.position,
            bb_result=self.current_bb,
            signal=signal.signal,
            signal_reason=signal.reason,
            last_signal_distance=signal.near_distance_pct,
            bot_status="RUNNING" if self.running else "STOPPING",
            cycle_count=self.cycle_count,
            last_error=self.last_error,
            paper_trading=self.cfg.paper_trading,
            risk_manager=self.risk_manager,
            recent_trades=recent_trades,
        )

    # ─── RUN LOOP ───

    def run(self):
        """Main bot loop."""
        logger.info("=" * 60)
        logger.info("🚀 BOLLINGER BAND REVERSAL BOT STARTING")
        logger.info("=" * 60)
        logger.info(f"Exchange: SharkEx v1")
        logger.info(f"Symbol: {self.cfg.exchange.symbol}")
        logger.info(f"Timeframe: {self.cfg.strategy.candle_tf}")
        logger.info(f"Paper Trading: {self.cfg.paper_trading}")
        logger.info(f"Trade Size: ₹{self.cfg.risk.trade_size_inr:,.0f}")
        logger.info(f"Max Daily Loss: ₹{self.cfg.risk.max_daily_loss_inr:,.0f}")
        logger.info(f"Max Trades/Day: {self.cfg.risk.max_trades_per_day}")
        logger.info(f"Poll Interval: {self.cfg.poll_interval_sec}s")
        logger.info(f"BB Period: {self.cfg.strategy.bb_period} | "
                    f"StdDev: {self.cfg.strategy.bb_stddev} | "
                    f"Near: {self.cfg.strategy.near_threshold*100:.1f}% | "
                    f"Trail: {self.cfg.strategy.trail_pct*100:.1f}%")
        logger.info("=" * 60)

        # Start live display
        try:
            self.display.start_live_display()
        except Exception as e:
            logger.warning(f"Live display start failed: {e}. Running in log-only mode.")

        logger.info("Bot loop started. Press Ctrl+C to stop.")

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
        print("   🤖 BOLLINGER BAND REVERSAL BOT - LIVE")
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
        description="5-Minute Bollinger Band Reversal Bot for SharkEx"
    )
    parser.add_argument(
        "--paper", action="store_true",
        help="Run in paper trading mode (no real orders)"
    )
    parser.add_argument(
        "--config", type=str, default=None,
        help="Path to a .env config file"
    )
    parser.add_argument(
        "--no-interactive", action="store_true",
        help="Skip interactive setup, use .env or defaults"
    )
    args = parser.parse_args()

    # Load .env file into os.environ before reading config.
    # find_dotenv() walks up from CWD looking for .env — the most robust
    # approach.  Also tries __file__-relative as explicit fallback.
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

    # Override paper trading from CLI
    if args.paper:
        cfg.paper_trading = True

    # Validate API keys
    if not cfg.paper_trading and (not cfg.exchange.api_key or not cfg.exchange.api_secret):
        print("\n⚠️  ERROR: API key and secret are required for live trading!")
        print("   Run with --paper for paper trading mode, or")
        print("   set SHARKEX_API_KEY and SHARKEX_API_SECRET in .env")
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