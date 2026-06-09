"""
Configuration module - Pure SharkEx v1, 24/7 trading.
All config values are loaded from environment variables or set via interactive CLI.
No session time restrictions.
"""

import os
import sys
from dataclasses import dataclass, field
from typing import Optional


# ===========================================================================
#  Strategy Configuration
# ===========================================================================

@dataclass
class StrategyConfig:
    """Bollinger Band reversal strategy parameters."""

    bb_period: int = 20
    """Number of candles for Bollinger Band calculation."""

    bb_stddev: float = 2.0
    """Standard deviation multiplier for Bollinger Bands."""

    candle_index: int = -2
    """Which closed candle to check for band touch (-2 = second-last closed)."""

    candle_tf: str = "5m"
    """Candle timeframe (5m, 15m, 1h, etc.)."""

    data_window: int = 100
    """Number of candles to fetch for analysis (minimum bb_period + 50)."""

    display_window: int = 30
    """Number of candles to show in display."""

    near_threshold: float = 0.005
    """How close candle close must be to band (0.005 = 0.5%)."""

    trail_pct: float = 0.005
    """Trailing stop percentage as decimal (0.005 = 0.5%)."""


# ===========================================================================
#  Risk Management Configuration
# ===========================================================================

@dataclass
class RiskConfig:
    """Position sizing and risk parameters."""

    trade_size_inr: float = 20000.0
    """Notional trade size in INR."""

    usd_inr_rate: float = 87.0
    """Reference USD/INR conversion rate."""

    trail_pct: float = 0.5
    """Trailing stop percentage (0.5 = 0.5%)."""

    max_daily_trades: int = 10
    """Maximum trades per day."""

    max_daily_loss_pct: float = 5.0
    """Stop if daily loss exceeds this % of trade capital."""

    @property
    def max_daily_loss_inr(self) -> float:
        """Maximum daily loss in INR."""
        return self.trade_size_inr * (self.max_daily_loss_pct / 100)

    @property
    def max_trades_per_day(self) -> int:
        """Alias for max_daily_trades (backward compat)."""
        return self.max_daily_trades


# ===========================================================================
#  Exchange Configuration
# ===========================================================================

@dataclass
class ExchangeConfig:
    """SharkEx exchange connection parameters."""

    exchange: str = "sharkex"
    """Exchange identifier (always sharkex)."""

    api_key: str = ""
    """SharkEx API key."""

    api_secret: str = ""
    """SharkEx API secret."""

    symbol: str = "BTC/USDT"
    """Trading pair."""

    order_book_depth: int = 5
    """Order book depth for price checks."""


# ===========================================================================
#  Master Bot Configuration
# ===========================================================================

@dataclass
class BotConfig:
    """
    Master configuration combining all sub-configs.

    Use :meth:`from_env` to load from environment variables.
    Use :meth:`interactive_setup` for the interactive CLI wizard.
    """

    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    exchange: ExchangeConfig = field(default_factory=ExchangeConfig)
    poll_interval_sec: float = 5.0
    """Seconds between strategy evaluation cycles."""
    log_level: str = "INFO"
    """Python logging level."""
    debug: bool = False
    """Enable debug mode (more verbose output)."""
    paper_trading: bool = False
    """Paper trading mode enabled when API keys are invalid or missing."""

    @classmethod
    def from_env(cls) -> "BotConfig":
        """Create config from environment variables (no interactive prompts)."""
        config = cls()
        config.exchange.api_key = os.getenv("SHARKEX_API_KEY", "")
        config.exchange.api_secret = os.getenv("SHARKEX_API_SECRET", "")
        config.exchange.symbol = os.getenv("TRADING_SYMBOL", "BTC/USDT")

        config.strategy.bb_period = int(os.getenv("BB_PERIOD", "20"))
        config.strategy.bb_stddev = float(os.getenv("BB_STDDEV", "2.0"))
        config.strategy.candle_tf = os.getenv("CANDLE_TF", "5m")
        config.strategy.data_window = int(os.getenv("DATA_WINDOW", "100"))
        config.strategy.display_window = int(os.getenv("DISPLAY_WINDOW", "30"))
        config.strategy.near_threshold = float(os.getenv("NEAR_THRESHOLD", "0.005"))
        config.strategy.trail_pct = float(os.getenv("STRATEGY_TRAIL_PCT", "0.005"))

        config.risk.trade_size_inr = float(os.getenv("TRADE_SIZE_INR", "20000"))
        config.risk.usd_inr_rate = float(os.getenv("USD_INR_RATE", "87"))
        config.risk.trail_pct = float(os.getenv("TRAIL_PCT", "0.5"))
        config.risk.max_daily_trades = int(os.getenv("MAX_DAILY_TRADES", "10"))
        config.risk.max_daily_loss_pct = float(os.getenv("MAX_DAILY_LOSS_PCT", "5"))

        config.poll_interval_sec = float(os.getenv("POLL_INTERVAL_SEC", "5"))
        config.log_level = os.getenv("LOG_LEVEL", "INFO")
        config.debug = os.getenv("DEBUG", "").lower() in ("1", "true", "yes")
        config.paper_trading = os.getenv("PAPER_TRADING", "").lower() in ("1", "true", "yes")

        return config

    @classmethod
    def interactive_setup(cls) -> "BotConfig":
        """
        Interactive configuration wizard (minimal prompts).
        Only asks for SharkEx API credentials if not already in environment.
        """
        config = cls()

        print("\n" + "=" * 60)
        print("  🦈 SHARKEX TRADING BOT — Setup")
        print("=" * 60)

        # --- Exchange ---
        api_key = os.getenv("SHARKEX_API_KEY", "")
        api_secret = os.getenv("SHARKEX_API_SECRET", "")

        if not api_key:
            api_key = input("Enter SharkEx API key: ").strip()
        if not api_secret:
            api_secret = input("Enter SharkEx API secret: ").strip()

        config.exchange.api_key = api_key
        config.exchange.api_secret = api_secret

        # --- Symbol ---
        symbol = input(f"Trading pair [{config.exchange.symbol}]: ").strip()
        if symbol:
            config.exchange.symbol = symbol.upper()

        print(f"\n  Exchange: SharkEx v1  |  Symbol: {config.exchange.symbol}")
        print("  Strategy: Bollinger Band Reversal (BB 20, 2σ)")
        print(f"  Trade Size: ₹{config.risk.trade_size_inr:,.0f}  |  Trail SL: {config.risk.trail_pct}%")
        print(f"  Mode: 24/7 — no session time restrictions")
        print("=" * 60 + "\n")

        return config