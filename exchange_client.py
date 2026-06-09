"""
exchange_client.py - SharkEx Exchange API Wrapper

Handles all communication with SharkEx (spot) exchange.
Uses direct REST API calls with HMAC-SHA256 signing.
Reference: https://docs.sharkexchange.in/#change-log

For Binance Futures Testnet, uses CCXT library.
"""

import time
import hmac
import hashlib
import json
import logging
import urllib.parse
from typing import Optional, Dict, List, Any, Tuple
from dataclasses import dataclass, field
from enum import Enum

import requests
import ccxt

from config import BotConfig

logger = logging.getLogger("exchange_client")


# =============================================================================
# Data Classes
# =============================================================================

class OrderSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"
    STOP_MARKET = "stop_market"
    STOP_LIMIT = "stop_limit"


class OrderStatus(str, Enum):
    OPEN = "open"
    FILLED = "filled"
    CANCELLED = "cancelled"
    PARTIALLY_FILLED = "partial"
    REJECTED = "rejected"


@dataclass
class Order:
    id: str
    symbol: str
    side: OrderSide
    order_type: OrderType
    price: float = 0.0
    amount: float = 0.0
    filled: float = 0.0
    remaining: float = 0.0
    status: OrderStatus = OrderStatus.OPEN
    timestamp: float = 0.0
    average_price: float = 0.0


@dataclass
class Position:
    symbol: str
    side: OrderSide                      # BUY = long, SELL = short
    entry_price: float
    quantity: float
    usdt_invested: float
    inr_invested: float
    entry_time: float
    highest_price: float = 0.0           # For trailing stop (long)
    lowest_price: float = 0.0            # For trailing stop (short)
    trailing_stop_price: float = 0.0
    stop_order_id: str = ""
    take_profit_target: float = 0.0

    @property
    def unrealized_pnl(self) -> float:
        return 0.0  # Overwritten by bot with live price

    @property
    def unrealized_pnl_pct(self) -> float:
        if self.usdt_invested == 0:
            return 0.0
        return (self.unrealized_pnl / self.usdt_invested) * 100


# =============================================================================
# SharkEx Direct API Client (Spot)
# =============================================================================

class SharkExClient:
    """
    Direct REST API client for SharkEx exchange.
    Uses HMAC-SHA256 signing for authenticated endpoints.
    
    Public market data (OHLCV, ticker) is fetched from Binance via CCXT
    as a fallback since SharkEx API routes are subject to change.
    BTC/USDT pricing is universal across exchanges.
    Trading operations (orders, balances) use SharkEx directly.
    """

    BASE_URL = "https://api.sharkexchange.in"

    def __init__(self, api_key: str, api_secret: str, symbol: str = "BTC/USDT"):
        self.api_key = api_key
        self.api_secret = api_secret
        self.symbol = symbol
        self.base_symbol = symbol.split("/")[0]   # BTC
        self.quote_symbol = symbol.split("/")[1]  # USDT
        self.market_symbol = f"{self.base_symbol}{self.quote_symbol}"  # BTCUSDT
        self.session = requests.Session()
        self.session.headers.update({
            "Content-Type": "application/json",
            "X-API-KEY": self.api_key,
        })
        
        # CCXT Binance fallback for public market data
        self._binance = None
        try:
            self._binance = ccxt.binance({
                'enableRateLimit': True,
                'options': {'defaultType': 'spot'},
            })
            self._binance.load_markets()
            logger.info("SharkExClient: Binance CCXT fallback initialized for public data")
        except Exception as e:
            logger.warning(f"SharkExClient: Binance CCXT fallback unavailable ({e}). "
                          "SharkEx public endpoints will be used directly.")

    def _sign(self, method: str, path: str, params: dict = None) -> str:
        """Generate HMAC-SHA256 signature"""
        if params is None:
            params = {}
        query_string = urllib.parse.urlencode(sorted(params.items())) if params else ""
        message = f"{method.upper()}{path}{query_string}"
        signature = hmac.new(
            self.api_secret.encode("utf-8"),
            message.encode("utf-8"),
            hashlib.sha256
        ).hexdigest()
        return signature

    def _request(self, method: str, path: str, params: dict = None, 
                 data: dict = None, signed: bool = False) -> dict:
        """Make HTTP request to SharkEx API"""
        url = f"{self.BASE_URL}{path}"
        
        headers = {}
        if signed:
            signature = self._sign(method, path, params)
            headers["X-SIGNATURE"] = signature

        try:
            if method.upper() == "GET":
                resp = self.session.get(url, params=params, headers=headers, timeout=15)
            elif method.upper() == "POST":
                resp = self.session.post(url, params=params, json=data, headers=headers, timeout=15)
            elif method.upper() == "DELETE":
                resp = self.session.delete(url, params=params, headers=headers, timeout=15)
            else:
                raise ValueError(f"Unsupported HTTP method: {method}")

            if resp.status_code == 429:
                logger.warning("Rate limited! Waiting 2 seconds...")
                time.sleep(2)
                return self._request(method, path, params, data, signed)

            resp.raise_for_status()
            
            result = resp.json()
            # SharkEx typically wraps responses in {success: true, data: {...}}
            if isinstance(result, dict) and "data" in result:
                return result["data"]
            return result

        except requests.exceptions.RequestException as e:
            logger.error(f"SharkEx API error [{method} {path}]: {e}")
            return {"error": str(e)}
        except json.JSONDecodeError:
            logger.error(f"Invalid JSON response from SharkEx for {method} {path}")
            return {"error": "Invalid JSON response"}

    # ---- PUBLIC ENDPOINTS ----

    def fetch_ticker(self) -> dict:
        """Fetch current ticker for the symbol.
        GET /api/v2/tickers
        """
        path = "/api/v2/tickers"
        result = self._request("GET", path)
        
        if isinstance(result, dict):
            # If result has the ticker for our market_symbol
            ticker_key = self.market_symbol.lower()
            if ticker_key in result:
                return result[ticker_key]
            # If it's a list-style response
            if "ticker" in result:
                ticker_data = result["ticker"]
                if "last" in ticker_data:
                    return ticker_data
            # Try common fields
            if "last" in result:
                return result
        return result

    def fetch_ohlcv(self, timeframe: str = "5m", limit: int = 100,
                    since: int = None) -> List[List[float]]:
        """Fetch OHLCV (candlestick) data.
        Uses Binance CCXT as primary source (universal BTC/USDT pricing).
        Falls back to SharkEx API if CCXT is unavailable.
        Returns: [[timestamp, open, high, low, close, volume], ...]
        """
        # --- Primary: Binance CCXT ---
        if self._binance:
            try:
                ohlcv = self._binance.fetch_ohlcv(
                    self.symbol, timeframe=timeframe, limit=limit, since=since
                )
                if ohlcv and len(ohlcv) > 0:
                    return ohlcv
            except Exception as e:
                logger.warning(f"Binance OHLCV fetch failed: {e}, trying SharkEx...")

        # --- Fallback: SharkEx direct API ---
        path = "/api/v2/klines"
        params = {
            "market": self.market_symbol.lower(),
            "period": self._timeframe_to_minutes(timeframe),
            "limit": limit,
        }
        if since:
            params["time_from"] = since

        result = self._request("GET", path, params=params)

        if isinstance(result, list):
            ohlcv = []
            for candle in result:
                if isinstance(candle, list) and len(candle) >= 5:
                    ohlcv.append(candle)
                elif isinstance(candle, dict):
                    ts = candle.get("time", candle.get("t", 0))
                    if isinstance(ts, str):
                        ts = int(ts) if ts.isdigit() else 0
                    ohlcv.append([
                        int(ts) * 1000 if ts < 10000000000 else int(ts),
                        float(candle.get("open", candle.get("o", 0))),
                        float(candle.get("high", candle.get("h", 0))),
                        float(candle.get("low", candle.get("l", 0))),
                        float(candle.get("close", candle.get("c", 0))),
                        float(candle.get("volume", candle.get("v", 0))),
                    ])
            return ohlcv

        logger.error(f"Unexpected OHLCV response: {result}")
        return []

    def fetch_current_price(self) -> float:
        """Fetch the last traded price for the symbol.
        Uses Binance CCXT as primary source, falls back to SharkEx ticker.
        """
        if self._binance:
            try:
                ticker = self._binance.fetch_ticker(self.symbol)
                if ticker and ticker.get('last'):
                    return float(ticker['last'])
            except Exception as e:
                logger.warning(f"Binance ticker fetch failed: {e}, trying SharkEx...")

        ticker = self.fetch_ticker()
        if isinstance(ticker, dict):
            price = ticker.get("last", ticker.get("last_price", ticker.get("close", 0)))
            return float(price) if price else 0.0
        elif isinstance(ticker, list) and len(ticker) > 0:
            t = ticker[0]
            if isinstance(t, dict):
                return float(t.get("last", t.get("close", 0)))
        return 0.0

    # ---- PRIVATE/AUTHENTICATED ENDPOINTS ----

    def fetch_balance(self) -> dict:
        """Fetch account balances.
        GET /api/v2/account/balances
        """
        path = "/api/v2/account/balances"
        result = self._request("GET", path, signed=True)
        
        if isinstance(result, dict):
            balances = {}
            for currency, details in result.items():
                if isinstance(details, dict):
                    balances[currency.upper()] = {
                        "free": float(details.get("available", details.get("free", 0))),
                        "used": float(details.get("locked", details.get("frozen", details.get("used", 0)))),
                        "total": float(details.get("balance", details.get("total", 0))),
                    }
            if balances:
                return balances
            
            # Alternative format
            if "balances" in result:
                for b in result["balances"]:
                    curr = b.get("currency", b.get("asset", "")).upper()
                    balances[curr] = {
                        "free": float(b.get("free", b.get("available", 0))),
                        "used": float(b.get("locked", b.get("frozen", 0))),
                        "total": float(b.get("total", b.get("balance", 0))),
                    }
            return balances

        return {}

    def fetch_free_balance(self, currency: str) -> float:
        """Get free (available) balance for a specific currency."""
        balances = self.fetch_balance()
        currency = currency.upper()
        if currency in balances:
            return balances[currency].get("free", 0.0)
        return 0.0

    def create_market_order(self, side: str, quantity: float) -> Optional[Order]:
        """Create a market order.
        POST /api/v2/orders
        
        Args:
            side: 'buy' or 'sell'
            quantity: Amount in base currency (BTC)
        """
        path = "/api/v2/orders"
        data = {
            "market": self.market_symbol.lower(),
            "side": side,
            "volume": str(quantity),
            "ord_type": "market",
        }

        result = self._request("POST", path, data=data, signed=True)

        if isinstance(result, dict) and "id" in result:
            return Order(
                id=str(result.get("id")),
                symbol=self.symbol,
                side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
                order_type=OrderType.MARKET,
                amount=quantity,
                filled=float(result.get("executed_volume", result.get("filled", quantity))),
                remaining=float(result.get("remaining_volume", result.get("remaining", 0))),
                status=OrderStatus.FILLED if result.get("state") in ("done", "filled") else OrderStatus.OPEN,
                timestamp=time.time(),
                average_price=float(result.get("avg_price", result.get("price", 0))),
            )

        logger.error(f"Failed to create market order: {result}")
        return None

    def create_stop_order(self, side: str, quantity: float, 
                          stop_price: float, limit_price: float = None) -> Optional[Order]:
        """Create a stop-loss order (stop-market or stop-limit).
        POST /api/v2/orders
        
        Args:
            side: 'buy' or 'sell' (opposite of position for stop)
            quantity: Amount in base currency
            stop_price: Trigger/stop price
            limit_price: Optional limit price (for stop-limit orders)
        """
        path = "/api/v2/orders"
        data = {
            "market": self.market_symbol.lower(),
            "side": side,
            "volume": str(quantity),
            "ord_type": "stop_limit" if limit_price else "stop_market",
            "price": str(limit_price or stop_price),
            "trigger_price": str(stop_price),
        }

        result = self._request("POST", path, data=data, signed=True)

        if isinstance(result, dict) and "id" in result:
            return Order(
                id=str(result.get("id")),
                symbol=self.symbol,
                side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
                order_type=OrderType.STOP_MARKET if not limit_price else OrderType.STOP_LIMIT,
                price=stop_price,
                amount=quantity,
                status=OrderStatus.OPEN,
                timestamp=time.time(),
            )

        # Fallback: try stop-limit
        logger.warning(f"Stop-market failed ({result}), trying stop-limit...")
        if not limit_price:
            buffer = 0.001  # 0.1% buffer
            if side == "sell":
                limit_price = stop_price * (1 - buffer)
            else:
                limit_price = stop_price * (1 + buffer)
            data["ord_type"] = "stop_limit"
            data["price"] = str(limit_price)
            data["trigger_price"] = str(stop_price)

            result = self._request("POST", path, data=data, signed=True)
            if isinstance(result, dict) and "id" in result:
                return Order(
                    id=str(result.get("id")),
                    symbol=self.symbol,
                    side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
                    order_type=OrderType.STOP_LIMIT,
                    price=stop_price,
                    amount=quantity,
                    status=OrderStatus.OPEN,
                    timestamp=time.time(),
                )

        logger.error(f"Failed to create stop order: {result}")
        return None

    def cancel_order(self, order_id: str) -> bool:
        """Cancel an existing order.
        DELETE /api/v2/order
        """
        path = "/api/v2/order/delete"
        params = {"id": order_id}

        result = self._request("POST", path, params=params, signed=True)

        if isinstance(result, dict):
            if result.get("error"):
                logger.error(f"Cancel failed for {order_id}: {result.get('error')}")
                return False
            return True

        return "error" not in str(result).lower()

    def fetch_order(self, order_id: str) -> Optional[Order]:
        """Fetch a specific order by ID.
        GET /api/v2/order
        """
        path = "/api/v2/order"
        params = {"id": order_id}

        result = self._request("GET", path, params=params, signed=True)

        if isinstance(result, dict) and "id" in result:
            side = result.get("side", "buy")
            return Order(
                id=str(result.get("id")),
                symbol=self.symbol,
                side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
                order_type=OrderType(result.get("ord_type", "market")),
                price=float(result.get("price", 0)),
                amount=float(result.get("volume", 0)),
                filled=float(result.get("executed_volume", result.get("filled", 0))),
                remaining=float(result.get("remaining_volume", result.get("remaining", 0))),
                status=OrderStatus(result.get("state", "open")),
                timestamp=time.time(),
                average_price=float(result.get("avg_price", 0)),
            )
        return None

    def _timeframe_to_minutes(self, tf: str) -> int:
        """Convert CCXT timeframe format to minutes."""
        mapping = {
            "1m": 1, "5m": 5, "15m": 15, "30m": 30,
            "1h": 60, "2h": 120, "4h": 240,
            "1d": 1440, "1w": 10080,
        }
        return mapping.get(tf, 5)


# =============================================================================
# Binance Futures Testnet Client
# =============================================================================

class BinanceFuturesClient:
    """CCXT-based client for Binance Futures Testnet."""

    def __init__(self, api_key: str, api_secret: str, symbol: str = "BTC/USDT"):
        self.exchange = ccxt.binance({
            "apiKey": api_key,
            "secret": api_secret,
            "options": {
                "defaultType": "future",
            },
            "urls": {
                "api": {
                    "public": "https://testnet.binancefuture.com/fapi/v1",
                    "private": "https://testnet.binancefuture.com/fapi/v1",
                },
            },
        })
        self.exchange.set_sandbox_mode(True)
        self.symbol = symbol
        self.base_symbol = symbol.split("/")[0]
        self.quote_symbol = symbol.split("/")[1]
        self.market_symbol = f"{self.base_symbol}{self.quote_symbol}"

    def fetch_ticker(self) -> dict:
        try:
            return self.exchange.fetch_ticker(self.symbol)
        except Exception as e:
            logger.error(f"Binance ticker error: {e}")
            return {}

    def fetch_ohlcv(self, timeframe: str = "5m", limit: int = 100,
                    since: int = None) -> List[List[float]]:
        try:
            return self.exchange.fetch_ohlcv(
                self.symbol, timeframe=timeframe, limit=limit, since=since
            )
        except Exception as e:
            logger.error(f"Binance OHLCV error: {e}")
            return []

    def fetch_current_price(self) -> float:
        ticker = self.fetch_ticker()
        return float(ticker.get("last", ticker.get("close", 0))) if ticker else 0.0

    def fetch_balance(self) -> dict:
        try:
            bal = self.exchange.fetch_balance()
            result = {}
            if "USDT" in bal:
                result["USDT"] = {
                    "free": float(bal["USDT"].get("free", 0)),
                    "used": float(bal["USDT"].get("used", 0)),
                    "total": float(bal["USDT"].get("total", 0)),
                }
            if self.base_symbol in bal:
                result[self.base_symbol] = {
                    "free": float(bal[self.base_symbol].get("free", 0)),
                    "used": float(bal[self.base_symbol].get("used", 0)),
                    "total": float(bal[self.base_symbol].get("total", 0)),
                }
            return result
        except Exception as e:
            logger.error(f"Binance balance error: {e}")
            return {}

    def fetch_free_balance(self, currency: str) -> float:
        balances = self.fetch_balance()
        currency = currency.upper()
        if currency in balances:
            return balances[currency].get("free", 0.0)
        return 0.0

    def create_market_order(self, side: str, quantity: float) -> Optional[Order]:
        try:
            self.exchange.set_leverage(1, self.symbol)  # 1x spot-like
            result = self.exchange.create_order(
                self.symbol, "market", side, quantity
            )
            return Order(
                id=result.get("id", ""),
                symbol=self.symbol,
                side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
                order_type=OrderType.MARKET,
                amount=quantity,
                filled=float(result.get("filled", quantity)),
                status=OrderStatus.FILLED if result.get("status") == "closed" else OrderStatus.OPEN,
                timestamp=time.time(),
                average_price=float(result.get("average", result.get("price", 0))),
            )
        except Exception as e:
            logger.error(f"Binance market order error: {e}")
            return None

    def create_stop_order(self, side: str, quantity: float,
                          stop_price: float, limit_price: float = None) -> Optional[Order]:
        try:
            params = {
                "stopPrice": stop_price,
            }
            order_type = "STOP_MARKET"
            
            result = self.exchange.create_order(
                self.symbol, order_type, side, quantity, None, params
            )
            return Order(
                id=result.get("id", ""),
                symbol=self.symbol,
                side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
                order_type=OrderType.STOP_MARKET,
                price=stop_price,
                amount=quantity,
                status=OrderStatus.OPEN,
                timestamp=time.time(),
            )
        except Exception as e:
            logger.error(f"Binance stop order error: {e}")
            return None

    def cancel_order(self, order_id: str) -> bool:
        try:
            self.exchange.cancel_order(order_id, self.symbol)
            return True
        except Exception as e:
            logger.error(f"Binance cancel error: {e}")
            return False

    def fetch_order(self, order_id: str) -> Optional[Order]:
        try:
            result = self.exchange.fetch_order(order_id, self.symbol)
            return Order(
                id=result.get("id", ""),
                symbol=self.symbol,
                side=OrderSide.BUY if result.get("side") == "buy" else OrderSide.SELL,
                order_type=OrderType(result.get("type", "market")),
                price=float(result.get("price", 0)),
                amount=float(result.get("amount", 0)),
                filled=float(result.get("filled", 0)),
                remaining=float(result.get("remaining", 0)),
                status=OrderStatus(result.get("status", "open")),
                timestamp=time.time(),
                average_price=float(result.get("average", 0)),
            )
        except Exception as e:
            logger.error(f"Binance fetch order error: {e}")
            return None


# =============================================================================
# Exchange Client Factory
# =============================================================================

def create_exchange_client(cfg: BotConfig):
    """
    Factory function to create the appropriate exchange client
    based on configuration.
    """
    if cfg.exchange.exchange_name == "sharkex":
        logger.info("Creating SharkEx client (Spot, Long Only)...")
        return SharkExClient(
            api_key=cfg.exchange.api_key,
            api_secret=cfg.exchange.api_secret,
            symbol=cfg.exchange.symbol,
        )
    elif cfg.exchange.exchange_name == "binance":
        logger.info("Creating Binance Futures Testnet client...")
        return BinanceFuturesClient(
            api_key=cfg.exchange.api_key,
            api_secret=cfg.exchange.api_secret,
            symbol=cfg.exchange.symbol,
        )
    else:
        raise ValueError(f"Unsupported exchange: {cfg.exchange.exchange_name}")