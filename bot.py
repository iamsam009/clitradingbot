"""
bot.py - Bollinger Band Reversal Trading Bot (Main Engine)

Orchestrates all modules:
- Exchange connection & data fetching
- Session checking (IST trading windows)
- Strategy signal detection
- Order execution & trailing stop management
- Risk management enforcement
- Live CLI display

Usage:
    python bot.py          # Interactive setup (asks for API keys)
    python bot.py --paper  # Paper trading mode (no real orders)
"""

import os
import sys
import time
import signal
import logging
import traceback
from datetime import datetime, timedelta
from typing import Optional, Dict, Any

import pytz

from config import BotConfig, SessionConfig
from exchange_client import (
    create_exchange_client,
    SharkExClient,
    BinanceFuturesClient,
    Position,
    OrderSide,
    Order,
    OrderStatus,
)
from strategy import BollingerBandStrategy, BBResult, SignalResult
from risk_manager import RiskManager, TradeRecord
from cli_display import CLIDisplay

# ─── Logging Setup ───
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("bot.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("bot")

IST = pytz.timezone("Asia/Kolkata")


# =============================================================================
# Session Checker
# =============================================================================

class SessionChecker:
    """
    Checks if the current IST time falls within allowed trading windows.
    Windows:
      Morning:   09:30 - 12:00
      Afternoon: 13:00 - 15:30
      Evening:   19:00 - 22:00
    """

    def __init__(self, cfg: SessionConfig):
        self.cfg = cfg

    def _parse_time(self, time_str: str) -> datetime:
        """Parse 'HH:MM' string to a datetime.time object."""
        h, m = map(int, time_str.split(":"))
        return datetime.now(IST).replace(hour=h, minute=m, second=0, microsecond=0)

    def is_in_session(self) -> tuple[bool, str]:
        """
        Check if current time is within any trading session.
        
        Returns:
            (is_in_session: bool, session_name: str)
        """
        now = datetime.now(IST)

        # Morning session
        morning_start = self._parse_time(self.cfg.morning_start)
        morning_end = self._parse_time(self.cfg.morning_end)
        if morning_start <= now <= morning_end:
            return True, "Morning"

        # Afternoon session
        afternoon_start = self._parse_time(self.cfg.afternoon_start)
        afternoon_end = self._parse_time(self.cfg.afternoon_end)
        if afternoon_start <= now <= afternoon_end:
            return True, "Afternoon"

        # Evening session
        evening_start = self._parse_time(self.cfg.evening_start)
        evening_end = self._parse_time(self.cfg.evening_end)
        if evening_start <= now <= evening_end:
            return True, "Evening"

        return False, "Outside"

    def next_session_info(self) -> str:
        """Get info about the next upcoming session."""
        now = datetime.now(IST)

        sessions = [
            ("Morning", self._parse_time(self.cfg.morning_start)),
            ("Afternoon", self._parse_time(self.cfg.afternoon_start)),
            ("Evening", self._parse_time(self.cfg.evening_start)),
        ]

        for name, start in sessions:
            if now < start:
                wait = start - now
                return f"Next: {name} at {start.strftime('%H:%M')} IST (in {wait.seconds // 60}m)"

        # All sessions passed - next is tomorrow morning
        tomorrow = now + timedelta(days=1)
        next_start = tomorrow.replace(
            hour=int(self.cfg.morning_start.split(":")[0]),
            minute=int(self.cfg.morning_start.split(":")[1]),
            second=0, microsecond=0,
        )
        wait = next_start - now
        return f"Next: Tomorrow Morning at {self.cfg.morning_start} IST (in {wait.seconds // 3600}h {wait.seconds % 3600 // 60}m)"


# =============================================================================
# Main Trading Bot
# =============================================================================

class TradingBot:
    """
    Main bot class that orchestrates the entire trading system.
    """

    def __init__(self, cfg: BotConfig):
        self.cfg = cfg
        self.exchange = create_exchange_client(cfg)
        self.strategy = BollingerBandStrategy(cfg.strategy)
        self.risk_manager = RiskManager(cfg.risk)
        self.session_checker = SessionChecker(cfg.session)
        self.display = CLIDisplay(cfg)

        # State
        self.position: Optional[Position] = None
        self.current_price: float = 0.0
        self.current_bb: Optional[BBResult] = None
        self.cycle_count: int = 0
        self.running: bool = True
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
                timeframe=self.cfg.exchange.timeframe,
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

        if signal.signal == "SHORT" and self.cfg.exchange.long_only:
            logger.info("SHORT signal ignored (Long Only mode)")
            return False

        # Check risk limits
        if not self.risk_manager.can_enter_new_trade():
            logger.warning(f"Cannot enter: {self.risk_manager.lock_reason}")
            return False

        # Calculate position size
        trade_usdt = self.cfg.trade_size_usdt
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

    # ─── MAIN CYCLE ───

    def run_cycle(self):
        """
        Execute one complete cycle of the bot:
        1. Fetch data
        2. Check session
        3. Check exit conditions
        4. Detect and execute entry signals
        5. Update display
        """
        self.cycle_count += 1

        # 1. Daily reset check
        self.risk_manager.check_and_reset_daily()

        # 2. Fetch data
        if not self.fetch_data():
            self.display.update_data(
                last_error=self.last_error,
                bot_status="ERROR (Data)",
            )
            return

        # 3. Check session
        in_session, session_name = self.session_checker.is_in_session()
        session_info = f"{session_name} Session" if in_session else self.session_checker.next_session_info()

        # 4. Check exit conditions (always, even outside session)
        if self.position:
            self.check_and_execute_exit()

        # 5. Entry logic (only during session)
        if in_session and self.position is None:
            signal = self.strategy.detect_entry_signal()
            if signal.signal != "NONE":
                self.display.print_signal_detected(signal.signal, signal.reason)
                self.execute_entry(signal)

        # 6. Update display data
        self._update_display(in_session, session_info)

    def _update_display(self, in_session: bool, session_info: str):
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
            in_session=in_session,
            session_info=session_info,
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
        logger.info(f"Exchange: {self.cfg.exchange.exchange_name}")
        logger.info(f"Symbol: {self.cfg.exchange.symbol}")
        logger.info(f"Timeframe: {self.cfg.exchange.timeframe}")
        logger.info(f"Long Only: {self.cfg.exchange.long_only}")
        logger.info(f"Paper Trading: {self.cfg.paper_trading}")
        logger.info(f"Trade Size: ₹{self.cfg.risk.trade_size_inr:,.0f}")
        logger.info(f"Max Daily Loss: ₹{self.cfg.risk.max_daily_loss_inr:,.0f}")
        logger.info(f"Max Trades/Day: {self.cfg.risk.max_trades_per_day}")
        logger.info(f"Poll Interval: {self.cfg.poll_interval_seconds}s")
        logger.info(f"BB Period: {self.cfg.strategy.bb_period} | "
                    f"StdDev: {self.cfg.strategy.bb_std_dev} | "
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

                # Run one cycle
                self.run_cycle()

                # Refresh display
                try:
                    self.display.refresh()
                except Exception:
                    pass  # Display errors shouldn't stop the bot

                # Calculate sleep time
                elapsed = time.time() - cycle_start
                sleep_time = max(1, self.cfg.poll_interval_seconds - elapsed)

                # Sleep in small increments to allow quick shutdown
                while sleep_time > 0 and self.running:
                    time.sleep(min(1, sleep_time))
                    sleep_time -= 1

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
        print(f"   Exchange: {self.cfg.exchange.exchange_name} | "
              f"Symbol: {self.cfg.exchange.symbol} | "
              f"TF: {self.cfg.exchange.timeframe}")
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
        description="5-Minute Bollinger Band Reversal Bot for SharkEx/Binance"
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

    # Create bot first so we can check exchange health
    bot = TradingBot(cfg)
    
    # Check if SharkEx private API is down and auto-switch to paper trading
    if (not cfg.paper_trading
        and cfg.exchange.exchange_name == "sharkex"
        and isinstance(bot.exchange, SharkExClient)):
        _ = bot.exchange.fetch_balance()  # Triggers private API health check
        status = bot.exchange.private_api_status
        if not status["available"]:
            logger.warning("=" * 60)
            logger.warning("⚠️  SHARKEX PRIVATE API IS UNAVAILABLE!")
            logger.warning(f"   Reason: {status['reason']}")
            logger.warning("   Auto-switching to PAPER TRADING mode.")
            logger.warning("   The bot will run with simulated balances and trades.")
            logger.warning("   Public market data is fetched from Binance CCXT.")
            logger.warning("=" * 60)
            bot.cfg.paper_trading = True
            # Also print to CLI for visibility
            print("\n" + "=" * 70)
            print("⚠️  SHARKEX PRIVATE API OFFLINE - PAPER TRADING ENABLED")
            print("=" * 70)
            print(f"   Reason: {status['reason'][:120]}...")
            print("   Bot continues with simulated execution.")
            print("=" * 70 + "\n")
            time.sleep(3)

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