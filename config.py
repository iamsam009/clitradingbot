"""
Configuration module - BB Squeeze Breakout strategy on 15-min candles.
Live spot trading on SharkEx v1. Session-based (IST windows).

All config values are loaded from environment variables or set via interactive CLI.
"""

import os
from dataclasses import dataclass, field
from typing import Optional


# ===========================================================================
#  Strategy Configuration
# ===========================================================================

@dataclass
class StrategyConfig:
    """Bollinger Squeeze Breakout strategy parameters (15-min candle)."""

    bb_period: int = 20
    """Number of candles for Bollinger Band calculation."""

    bb_stddev: float = 2.0
    """Standard deviation multiplier for Bollinger Bands."""

    candle_tf: str = "15m"
    """Candle timeframe (always 15m for this strategy)."""

    data_window: int = 120
    """Number of candles to fetch for analysis (minimum bb_period + squeeze_lookback + 50)."""

    display_window: int = 40
    """Number of candles to show in display."""

    squeeze_lookback: int = 10
    """Number of candles for BB-width rolling minimum (squeeze detection)."""

    breakout_lookback: int = 10
    """Number of candles whose highest high the close must break for LONG entry."""

    trailing_lookback: int = 5
    """Number of candles whose lowest low forms the trailing stop."""

    limit_order_timeout: int = 10
    """Seconds to wait for a limit order fill before falling back to market."""

    short_enabled: bool = False
    """Enable SHORT entry signals. Set False for LONG-only spot trading."""


# ===========================================================================
#  Risk Management Configuration
# ===========================================================================

@dataclass
class RiskConfig:
    """Position sizing and risk parameters."""

    trade_size_inr: float = 20000.0
    """Notional trade size in INR."""

    usd_inr_rate: float = 83.5
    """Reference USD/INR conversion rate."""

    max_daily_trades: int = 30
    """Maximum trades (entries) allowed per calendar day."""

    max_daily_loss_inr: float = 3000.0
    """Maximum daily loss in INR. Bot stops entering new trades when this is hit."""

    @property
    def max_daily_loss_pct(self) -> float:
        """Maximum daily loss as % of trade capital (computed)."""
        return (self.max_daily_loss_inr / self.trade_size_inr) * 100 if self.trade_size_inr else 0.0

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
    """Trading pair (display format, e.g. BTC/USDT)."""

    contract_name: str = "BTCINR"
    """SharkEx contract name for leverage/margin calls (e.g. BTCINR)."""

    leverage: int = 1
    """Leverage multiplier (1x = spot)."""

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

        # -- exchange --------------------------------------------------------
        config.exchange.api_key = os.getenv("SHARKEX_API_KEY", "")
        config.exchange.api_secret = os.getenv("SHARKEX_API_SECRET", "")
        config.exchange.symbol = os.getenv("TRADING_SYMBOL", "BTC/USDT")
        config.exchange.contract_name = os.getenv("CONTRACT_NAME", "BTCINR")
        config.exchange.leverage = int(os.getenv("LEVERAGE", "1"))

        # -- strategy --------------------------------------------------------
        config.strategy.bb_period = int(os.getenv("BB_PERIOD", "20"))
        config.strategy.bb_stddev = float(os.getenv("BB_STDDEV", "2.0"))
        config.strategy.candle_tf = os.getenv("CANDLE_TF", "15m")
        config.strategy.data_window = int(os.getenv("DATA_WINDOW", "120"))
        config.strategy.display_window = int(os.getenv("DISPLAY_WINDOW", "40"))
        config.strategy.squeeze_lookback = int(os.getenv("SQUEEZE_LOOKBACK", "10"))
        config.strategy.breakout_lookback = int(os.getenv("BREAKOUT_LOOKBACK", "10"))
        config.strategy.trailing_lookback = int(os.getenv("TRAILING_LOOKBACK", "5"))
        config.strategy.limit_order_timeout = int(os.getenv("LIMIT_ORDER_TIMEOUT", "10"))
        config.strategy.short_enabled = os.getenv("SHORT_ENABLED", "false").lower() in ("1", "true", "yes")

        # -- risk ------------------------------------------------------------
        config.risk.trade_size_inr = float(os.getenv("TRADE_SIZE_INR", "20000"))
        config.risk.usd_inr_rate = float(os.getenv("USD_INR_RATE", "83.5"))
        config.risk.max_daily_trades = int(os.getenv("MAX_DAILY_TRADES", "30"))
        config.risk.max_daily_loss_inr = float(os.getenv("MAX_DAILY_LOSS_INR", "3000"))

        # -- bot -------------------------------------------------------------
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
        print("  Strategy: BB Squeeze Breakout (15-min, BB 20/2σ, squeeze=10, breakout=10, trail=5)")
        print(f"  Trade Size: ₹{config.risk.trade_size_inr:,.0f}  |  Leverage: {config.exchange.leverage}x (spot)")
        print("  Mode: Session-based (09:30-12:00, 13:00-15:30, 19:00-22:00 IST)")
        print(f"  SHORT signals: {'ON' if config.strategy.short_enabled else 'OFF (LONG only)'}")
        print("=" * 60 + "\n")

        return config