"""
config.py - Configuration Management for the Bollinger Band Reversal Bot

All configurable parameters, constants, and user input handling.
"""

import os
from dataclasses import dataclass, field
from typing import Optional
from dotenv import load_dotenv

load_dotenv()


@dataclass
class SessionConfig:
    """Trading session times in IST (Indian Standard Time)"""
    morning_start: str = "09:30"
    morning_end: str = "12:00"
    afternoon_start: str = "13:00"
    afternoon_end: str = "15:30"
    evening_start: str = "19:00"
    evening_end: str = "22:00"


@dataclass
class StrategyConfig:
    """Bollinger Band strategy parameters"""
    bb_period: int = 20
    bb_std_dev: float = 2.0
    near_threshold: float = 0.002          # 0.2% - "near" the band
    trail_pct: float = 0.005               # 0.5% trailing stop distance
    data_window: int = 50                  # candles to fetch
    display_window: int = 100              # candles for chart display


@dataclass
class RiskConfig:
    """Risk management parameters"""
    trade_size_inr: float = 20_000.0       # ₹20,000 per trade
    usd_inr_rate: float = 83.5             # USD to INR conversion rate
    max_daily_loss_inr: float = 3_000.0    # ₹3,000 max daily loss
    max_trades_per_day: int = 30
    leverage: int = 1                      # Spot-like (1x)


@dataclass
class ExchangeConfig:
    """Exchange connection config"""
    api_key: str = ""
    api_secret: str = ""
    exchange_name: str = "sharkex"
    symbol: str = "BTC/USDT"
    timeframe: str = "5m"
    long_only: bool = True                 # SharkEx spot = long only


@dataclass
class BotConfig:
    """Master configuration combining all sub-configs"""
    session: SessionConfig = field(default_factory=SessionConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    exchange: ExchangeConfig = field(default_factory=ExchangeConfig)
    poll_interval_seconds: int = 60        # How often to run a cycle
    paper_trading: bool = False

    @classmethod
    def from_env(cls) -> "BotConfig":
        """Load configuration from environment variables with defaults"""
        cfg = cls()
        cfg.exchange.api_key = os.getenv("SHARKEX_API_KEY", "")
        cfg.exchange.api_secret = os.getenv("SHARKEX_API_SECRET", "")
        cfg.exchange.exchange_name = os.getenv("EXCHANGE_NAME", "sharkex")
        cfg.exchange.symbol = os.getenv("SYMBOL", "BTC/USDT")

        cfg.strategy.bb_period = int(os.getenv("BB_PERIOD", "20"))
        cfg.strategy.bb_std_dev = float(os.getenv("BB_STD_DEV", "2.0"))
        cfg.strategy.near_threshold = float(os.getenv("NEAR_THRESHOLD", "0.002"))
        cfg.strategy.trail_pct = float(os.getenv("TRAIL_PCT", "0.005"))

        cfg.risk.trade_size_inr = float(os.getenv("TRADE_SIZE_INR", "20000"))
        cfg.risk.usd_inr_rate = float(os.getenv("USD_INR_RATE", "83.5"))
        cfg.risk.max_daily_loss_inr = float(os.getenv("MAX_DAILY_LOSS_INR", "3000"))
        cfg.risk.max_trades_per_day = int(os.getenv("MAX_TRADES_PER_DAY", "30"))

        cfg.poll_interval_seconds = int(os.getenv("POLL_INTERVAL", "60"))
        cfg.paper_trading = os.getenv("PAPER_TRADING", "false").lower() == "true"
        return cfg

    @classmethod
    def interactive_setup(cls) -> "BotConfig":
        """Interactive CLI setup - asks for API keys and all configs on first run"""
        cfg = cls()

        print("\n" + "=" * 70)
        print("   🚀 BOLLINGER BAND REVERSAL BOT - INITIAL SETUP")
        print("=" * 70)

        # Exchange selection
        print("\n📡 EXCHANGE SETUP")
        print("-" * 40)
        print("\n  ⚠️  NOTE: SharkEx API is currently undergoing migration")
        print("  (API routes changed from /api/v2 to /v1 with JWT auth).")
        print("  If SharkEx private API is down, the bot auto-switches to")
        print("  PAPER TRADING with Binance CCXT for live market data.")
        print("  Binance Futures Testnet is fully operational.\n")
        
        ex_choice = input("Exchange [sharkex / binance_futures_testnet] (default: sharkex): ").strip().lower()
        if ex_choice == "binance_futures_testnet":
            cfg.exchange.exchange_name = "binance"
            cfg.exchange.long_only = False
            cfg.exchange.symbol = "BTC/USDT"
            print("  -> Using Binance Futures Testnet (Long + Short enabled)")
        else:
            cfg.exchange.exchange_name = "sharkex"
            cfg.exchange.long_only = True
            cfg.exchange.symbol = "BTC/USDT"
            print("  -> Using SharkEx (Spot, Long Only)")
            print("  -> If SharkEx API is unavailable, paper trading will be used automatically.")

        cfg.exchange.api_key = input("API Key: ").strip()
        cfg.exchange.api_secret = input("API Secret: ").strip()

        # Paper trading
        paper = input("\nPaper trading mode? (yes/no, default: no): ").strip().lower()
        cfg.paper_trading = paper in ("yes", "y", "true")

        # Strategy config
        print("\n📊 STRATEGY CONFIGURATION")
        print("-" * 40)
        bb_period = input(f"BB Period (default {cfg.strategy.bb_period}): ").strip()
        if bb_period:
            cfg.strategy.bb_period = int(bb_period)

        bb_std = input(f"BB Std Dev (default {cfg.strategy.bb_std_dev}): ").strip()
        if bb_std:
            cfg.strategy.bb_std_dev = float(bb_std)

        near_thresh = input(f"Near-band threshold % (default {cfg.strategy.near_threshold*100}%): ").strip()
        if near_thresh:
            cfg.strategy.near_threshold = float(near_thresh) / 100.0

        trail = input(f"Trailing stop % (default {cfg.strategy.trail_pct*100}%): ").strip()
        if trail:
            cfg.strategy.trail_pct = float(trail) / 100.0

        # Risk config
        print("\n💰 RISK MANAGEMENT")
        print("-" * 40)
        trade_size = input(f"Trade size in ₹ (default {cfg.risk.trade_size_inr}): ").strip()
        if trade_size:
            cfg.risk.trade_size_inr = float(trade_size)

        usd_inr = input(f"USD/INR rate (default {cfg.risk.usd_inr_rate}): ").strip()
        if usd_inr:
            cfg.risk.usd_inr_rate = float(usd_inr)

        max_loss = input(f"Max daily loss in ₹ (default {cfg.risk.max_daily_loss_inr}): ").strip()
        if max_loss:
            cfg.risk.max_daily_loss_inr = float(max_loss)

        max_trades = input(f"Max trades per day (default {cfg.risk.max_trades_per_day}): ").strip()
        if max_trades:
            cfg.risk.max_trades_per_day = int(max_trades)

        # Session config
        print("\n🕐 TRADING SESSIONS (IST)")
        print("-" * 40)
        m_start = input(f"Morning start (default {cfg.session.morning_start}): ").strip()
        if m_start:
            cfg.session.morning_start = m_start
        m_end = input(f"Morning end (default {cfg.session.morning_end}): ").strip()
        if m_end:
            cfg.session.morning_end = m_end

        a_start = input(f"Afternoon start (default {cfg.session.afternoon_start}): ").strip()
        if a_start:
            cfg.session.afternoon_start = a_start
        a_end = input(f"Afternoon end (default {cfg.session.afternoon_end}): ").strip()
        if a_end:
            cfg.session.afternoon_end = a_end

        e_start = input(f"Evening start (default {cfg.session.evening_start}): ").strip()
        if e_start:
            cfg.session.evening_start = e_start
        e_end = input(f"Evening end (default {cfg.session.evening_end}): ").strip()
        if e_end:
            cfg.session.evening_end = e_end

        poll = input(f"\nPoll interval in seconds (default {cfg.poll_interval_seconds}): ").strip()
        if poll:
            cfg.poll_interval_seconds = int(poll)

        print("\n" + "=" * 70)
        print("   ✅ SETUP COMPLETE! Starting bot...")
        print("=" * 70 + "\n")
        return cfg

    @property
    def trade_size_usdt(self) -> float:
        return self.risk.trade_size_inr / self.risk.usd_inr_rate

    @property
    def max_daily_loss_usdt(self) -> float:
        return self.risk.max_daily_loss_inr / self.risk.usd_inr_rate