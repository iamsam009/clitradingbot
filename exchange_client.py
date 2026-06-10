"""
Exchange client module - Pure SharkEx API v1 integration.
Uses HMAC-SHA256 signature authentication as per SharkEx documentation:
  https://docs.sharkexchange.in/
Base URL: https://api.sharkexchange.in/v1/

Auth rules:
  - GET requests:  sign sorted query_string (including timestamp) -> headers: api-key, signature
  - POST/PUT/DELETE: sign JSON.stringify(body with timestamp) -> headers: api-key, signature
  - Public /v1/market/* endpoints: no auth required
"""

import hashlib
import hmac
import json
import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode, urljoin

import requests

from config import BotConfig

logger = logging.getLogger(__name__)

BASE_URL = "https://api.sharkexchange.in"


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP_MARKET = "STOP_MARKET"


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderStatus(str, Enum):
    NEW = "NEW"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"


@dataclass
class Order:
    """Represents an exchange order."""
    order_id: str
    client_order_id: str = ""
    symbol: str = ""
    side: str = ""
    order_type: str = ""
    price: float = 0.0
    stop_price: float = 0.0
    quantity: float = 0.0
    executed_qty: float = 0.0
    status: str = ""
    avg_price: float = 0.0
    raw: Optional[dict] = None

    def __post_init__(self):
        if self.raw is None:
            self.raw = {}

    @property
    def is_filled(self) -> bool:
        return self.status in ("FILLED", "filled")

    @property
    def is_open(self) -> bool:
        return self.status in ("NEW", "PARTIALLY_FILLED", "new", "partially_filled")


@dataclass
class Position:
    """Represents an open position with tracking fields for the bot."""
    # Core exchange fields
    symbol: str = ""
    side: str = ""                         # "BUY" or "SELL"
    quantity: float = 0.0
    entry_price: float = 0.0
    mark_price: float = 0.0
    unrealized_pnl: float = 0.0
    margin: float = 0.0
    raw: Optional[dict] = None

    # Bot tracking fields
    usdt_invested: float = 0.0
    inr_invested: float = 0.0
    entry_time: float = 0.0
    highest_price: float = 0.0
    lowest_price: float = 0.0
    trailing_stop_price: float = 0.0
    stop_order_id: str = ""

    def __post_init__(self):
        if self.raw is None:
            self.raw = {}

    @property
    def unrealized_pnl_pct(self) -> float:
        if self.margin > 0:
            return (self.unrealized_pnl / self.margin) * 100
        return 0.0


# ---------------------------------------------------------------------------
#  SharkEx v1 Client
# ---------------------------------------------------------------------------

class SharkExClient:
    """
    Pure SharkEx API v1 client using HMAC-SHA256 signature authentication.

    Public endpoints (no auth):
      - GET  /v1/market/ticker24Hr/{contractPair}
      - POST /v1/market/klines?priceType=MARK_PRICE
      - POST /v1/market/aggTrade
      - POST /v1/market/depth

    Authenticated endpoints:
      - POST   /v1/order/place-order
      - GET    /v1/order/open-orders
      - GET    /v1/order/{clientOrderId}
      - DELETE /v1/order/delete-order
      - DELETE /v1/order/cancel-all
      - GET    /v1/exchange/futures-wallet-details
      - GET    /v1/positions/get-positions
    """

    def __init__(self, api_key: str, api_secret: str, symbol: str = "BTC/USDT"):
        self.api_key = api_key
        self.api_secret = api_secret
        self.symbol = symbol                         # "BTC/USDT"
        self._market_symbol = symbol.replace("/", "")  # "BTCUSDT"
        self._session = requests.Session()
        self._session.headers.update({
            "Content-Type": "application/json",
            "Accept": "application/json",
        })
        self._last_request_time: float = 0.0
        self._min_request_interval: float = 0.05     # 50 ms rate-limit safety

    # -- identity -----------------------------------------------------------

    @property
    def exchange_name(self) -> str:
        return "SharkEx v1"

    @property
    def private_api_available(self) -> bool:
        return True

    @property
    def private_api_status(self) -> dict:
        return {"available": True, "mode": "live"}

    # -- rate limiting ------------------------------------------------------

    def _rate_limit(self):
        elapsed = time.time() - self._last_request_time
        if elapsed < self._min_request_interval:
            time.sleep(self._min_request_interval - elapsed)

    # -- auth helpers -------------------------------------------------------

    def _sign_get(self, params: dict) -> Tuple[str, dict]:
        """
        Sign a GET request.
        Sorts params + timestamp, signs the query string.
        Returns (query_string, headers).
        """
        params = dict(params)
        params["timestamp"] = str(int(time.time() * 1000))
        sorted_items = sorted(params.items())
        query_string = urlencode(sorted_items)
        signature = hmac.new(
            self.api_secret.encode("utf-8"),
            query_string.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        headers = {
            "api-key": self.api_key,
            "signature": signature,
        }
        return query_string, headers

    def _sign_body(self, params: dict) -> Tuple[str, dict]:
        """
        Sign a POST / PUT / DELETE request.
        Adds timestamp to body dict, signs the JSON string.
        Returns (body_json_string, headers).
        """
        params = dict(params)
        params["timestamp"] = str(int(time.time() * 1000))
        body = json.dumps(params)
        signature = hmac.new(
            self.api_secret.encode("utf-8"),
            body.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        headers = {
            "api-key": self.api_key,
            "signature": signature,
            "Content-Type": "application/json",
        }
        return body, headers

    # -- HTTP core ----------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[dict] = None,
        authenticated: bool = True,
        retries: int = 2,
    ) -> dict:
        """
        Make an HTTP request to the SharkEx v1 API.

        Parameters
        ----------
        method : str
            HTTP method (GET, POST, PUT, DELETE).
        path : str
            URL path relative to BASE_URL, e.g. "/v1/market/ticker24Hr/BTCUSDT".
        params : dict, optional
            Query-string or body parameters.
        authenticated : bool
            Whether the endpoint requires HMAC auth headers.
        retries : int
            Number of retry attempts on server errors.
        """
        url = urljoin(BASE_URL, path)
        params = dict(params or {})
        self._rate_limit()

        for attempt in range(retries + 1):
            try:
                if not authenticated:
                    # Public endpoints – simple request, no auth
                    if method in ("POST", "PUT"):
                        resp = self._session.post(
                            url, json=params, timeout=15
                        )
                    else:
                        resp = self._session.get(url, params=params, timeout=15)
                elif method == "GET":
                    query_string, headers = self._sign_get(params)
                    full_url = f"{url}?{query_string}"
                    resp = self._session.get(full_url, headers=headers, timeout=15)
                elif method in ("POST", "PUT", "DELETE"):
                    body, headers = self._sign_body(params)
                    resp = self._session.request(
                        method, url, data=body, headers=headers, timeout=15
                    )
                else:
                    resp = self._session.request(method, url, json=params, timeout=15)

                self._last_request_time = time.time()

                if resp.status_code in (200, 201):
                    return resp.json()
                elif resp.status_code == 429:
                    logger.warning("Rate-limited by SharkEx API. Waiting 5 s …")
                    time.sleep(5)
                    continue
                elif resp.status_code >= 500 and attempt < retries:
                    logger.warning(
                        f"SharkEx API server error {resp.status_code}, retrying ({attempt + 1}/{retries}) …"
                    )
                    time.sleep(1)
                    continue
                else:
                    logger.error(
                        f"SharkEx API error {resp.status_code} on {method} {path}: {resp.text[:300]}"
                    )
                    return {
                        "error": True,
                        "code": resp.status_code,
                        "message": resp.text[:300],
                    }

            except requests.exceptions.Timeout:
                logger.error(f"SharkEx API timeout on {method} {path}")
                if attempt < retries:
                    time.sleep(1)
                    continue
                return {"error": True, "code": -1, "message": "Request timeout"}
            except requests.exceptions.ConnectionError as e:
                logger.error(f"SharkEx API connection error: {e}")
                if attempt < retries:
                    time.sleep(2)
                    continue
                return {"error": True, "code": -2, "message": str(e)}
            except Exception as e:
                logger.error(f"SharkEx API unexpected error: {e}")
                return {"error": True, "code": -3, "message": str(e)}

        return {"error": True, "code": -99, "message": "Max retries exceeded"}

    # =======================================================================
    #  PUBLIC MARKET DATA
    # =======================================================================

    def fetch_ticker(self) -> dict:
        """
        GET /v1/market/ticker24Hr/{contractPair}  (public, no auth)

        Response fields: symbol, priceChange, priceChangePercent, lastPrice,
        lastQty, highPrice, lowPrice, volume, quoteVolume, openPrice,
        openTime, closeTime.
        """
        return self._request(
            "GET",
            f"/v1/market/ticker24Hr/{self._market_symbol}",
            authenticated=False,
        )

    def fetch_current_price(self) -> float:
        """Return the latest price from the 24hr ticker.

        SharkEx wraps the response in {"data": {...}} where the
        current price field is 'c' (Binance-style compact format).
        """
        try:
            ticker = self.fetch_ticker()
            if "error" in ticker:
                logger.error(f"Failed to fetch price: {ticker.get('message')}")
                return 0.0

            # SharkEx nests the actual ticker inside a "data" envelope:
            #   {"data": {"c": "5271387.66", "h": ..., "l": ..., ...}}
            data = ticker.get("data", ticker)

            # Try SharkEx field first ('c' = last/current price), then
            # fall back to 'lastPrice' for Binance-style responses
            price = data.get("c") or data.get("lastPrice", 0)
            return float(price)
        except Exception as e:
            logger.error(f"Error fetching current price: {e}")
            return 0.0

    def fetch_ohlcv(
        self,
        timeframe: str = "5m",
        limit: int = 100,
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """
        POST /v1/market/klines?priceType=MARK_PRICE  (public, no auth)

        Body: { symbol, interval, limit, startTime?, endTime? }

        Returns list of dicts with keys: timestamp, open, high, low, close, volume.
        """
        body: Dict[str, Any] = {
            "pair": self._market_symbol,
            "interval": timeframe,
            "limit": limit,
        }
        if start_time:
            body["startTime"] = start_time
        if end_time:
            body["endTime"] = end_time

        resp = self._request(
            "POST",
            "/v1/market/klines?priceType=MARK_PRICE",
            params=body,
            authenticated=False,
        )

        if "error" in resp:
            logger.error(f"Failed to fetch klines: {resp.get('message')}")
            return []

        # SharkEx returns klines as an array of objects:
        #   [{"startTime":"...", "open":"...", "high":"...", "low":"...",
        #     "close":"...", "endTime":"..."}]
        candles: List[Dict[str, Any]] = []
        data = resp if isinstance(resp, list) else resp.get("data", resp)
        if not isinstance(data, list):
            return candles

        for row in data:
            if isinstance(row, dict):
                candles.append({
                    "timestamp": int(row.get("startTime", row.get("timestamp", 0))),
                    "open": float(row.get("open", 0)),
                    "high": float(row.get("high", 0)),
                    "low": float(row.get("low", 0)),
                    "close": float(row.get("close", 0)),
                    "volume": float(row.get("volume", 0)),
                })
            elif isinstance(row, list) and len(row) >= 6:
                # Fallback: Binance-style array format
                candles.append({
                    "timestamp": int(row[0]),
                    "open": float(row[1]),
                    "high": float(row[2]),
                    "low": float(row[3]),
                    "close": float(row[4]),
                    "volume": float(row[5]),
                })
        return candles

    # =======================================================================
    #  ACCOUNT & WALLET
    # =======================================================================

    def fetch_balance(self) -> dict:
        """
        GET /v1/wallet/futures-wallet/details  (authenticated)

        SharkEx returns a flat wallet-level object.  Example (INR):
          {"inrBalance":"1941.50", "walletBalance":"1941.50", "marginBalance":..., ...}

        When the margin asset is USDT the keys become usdtBalance,
        walletBalance, etc.  We extract USDT free/total from the flat
        response and default everything else to zero.

        Returns dict like {"USDT": {"free": 123.45, "total": 123.45}, ...}
        """
        resp = self._request(
            "GET",
            "/v1/wallet/futures-wallet/details",
            authenticated=True,
        )

        if "error" in resp:
            logger.error(f"Failed to fetch balance: {resp.get('message')}")
            return {"USDT": {"free": 0, "total": 0}}

        # The response may be wrapped in {"data": {...}} or be bare.
        wallet = resp.get("data", resp)
        if not isinstance(wallet, dict):
            logger.warning(f"Unexpected futures-wallet response type: {type(resp)}")
            return {"USDT": {"free": 0, "total": 0}}

        # Try USDT-margin field names first, then INR fallback.
        # 'walletBalance' is always the total; the per-asset balance uses the
        # lowercase asset code (e.g. 'usdtBalance' or 'inrBalance').
        usdt_free = float(
            wallet.get("usdtBalance")
            or wallet.get("availableBalance")
            or 0
        )
        total = float(wallet.get("walletBalance", 0))

        balances: Dict[str, Dict[str, float]] = {}
        balances["USDT"] = {"free": usdt_free, "total": total}

        # Also expose any other scalar asset balances the response carries.
        for key, val in wallet.items():
            if isinstance(val, (int, float, str)) and key.endswith("Balance"):
                # e.g. "inrBalance", "btcBalance"
                asset = key[:-7].upper()  # "inrBalance" → "INR"
                if asset not in balances:
                    balances[asset] = {"free": float(val), "total": float(val)}

        return balances

    def fetch_free_balance(self, currency: str) -> float:
        """Return the free (available) balance for *currency*."""
        balances = self.fetch_balance()
        return balances.get(currency, {}).get("free", 0)

    # =======================================================================
    #  POSITIONS
    # =======================================================================

    def fetch_positions(self) -> List[Position]:
        """
        GET /v1/positions/get-positions  (authenticated)

        Returns list of Position objects for non-zero positions.
        """
        resp = self._request(
            "GET",
            "/v1/positions/get-positions",
            authenticated=True,
        )

        if "error" in resp:
            logger.error(f"Failed to fetch positions: {resp.get('message')}")
            return []

        # Response format per docs: { data: [ { symbol, positionAmt, entryPrice,
        #   markPrice, unRealizedProfit, ... }, ... ] }
        pos_list = resp.get("data", resp)
        if isinstance(pos_list, dict):
            pos_list = [pos_list]
        if not isinstance(pos_list, list):
            return []

        positions: List[Position] = []
        for p in pos_list:
            if not isinstance(p, dict):
                continue
            qty = float(p.get("positionAmt", p.get("quantity", 0)))
            if abs(qty) < 1e-8:
                continue
            positions.append(Position(
                symbol=p.get("symbol", self._market_symbol),
                side="BUY" if qty > 0 else "SELL",
                quantity=abs(qty),
                entry_price=float(p.get("entryPrice", 0)),
                mark_price=float(p.get("markPrice", 0)),
                unrealized_pnl=float(p.get("unRealizedProfit", p.get("unrealizedPnl", 0))),
                margin=float(p.get("margin", p.get("isolatedMargin", 0))),
                raw=p,
            ))

        return positions

    # =======================================================================
    #  ORDERS
    # =======================================================================

    def create_market_order(self, side: str, quantity: float) -> Optional[Order]:
        """
        POST /v1/order/place-order  (authenticated)

        Body: { placeType, quantity, side, symbol, type }
        """
        params = {
            "placeType": "order_type",
            "quantity": str(quantity),
            "side": side.upper(),
            "symbol": self._market_symbol,
            "type": "MARKET",
        }

        resp = self._request(
            "POST",
            "/v1/order/place-order",
            params=params,
            authenticated=True,
        )

        if "error" in resp:
            logger.error(f"Failed to create MARKET order: {resp.get('message')}")
            return None

        return self._parse_order_response(resp, side)

    def create_stop_order(
        self, side: str, quantity: float, stop_price: float
    ) -> Optional[Order]:
        """
        POST /v1/order/place-order  (authenticated)

        Body: { placeType, quantity, side, symbol, type, stopPrice }
        """
        params = {
            "placeType": "order_type",
            "quantity": str(quantity),
            "side": side.upper(),
            "symbol": self._market_symbol,
            "type": "STOP_MARKET",
            "stopPrice": str(stop_price),
        }

        resp = self._request(
            "POST",
            "/v1/order/place-order",
            params=params,
            authenticated=True,
        )

        if "error" in resp:
            logger.error(f"Failed to create STOP order: {resp.get('message')}")
            return None

        return self._parse_order_response(resp, side)

    def cancel_order(self, order_id: str) -> bool:
        """
        DELETE /v1/order/delete-order  (authenticated)

        Body: { clientOrderId }
        """
        params = {"clientOrderId": order_id}

        resp = self._request(
            "DELETE",
            "/v1/order/delete-order",
            params=params,
            authenticated=True,
        )

        if "error" in resp:
            logger.error(f"Failed to cancel order {order_id}: {resp.get('message')}")
            return False

        logger.info(f"Order cancelled: {order_id}")
        return True

    def fetch_order(self, order_id: str) -> Optional[Order]:
        """
        GET /v1/order/{clientOrderId}  (authenticated)

        Fetch order details by client order ID.
        """
        resp = self._request(
            "GET",
            f"/v1/order/{order_id}",
            authenticated=True,
        )

        if "error" in resp:
            logger.error(f"Failed to fetch order {order_id}: {resp.get('message')}")
            return None

        return self._parse_order_response(resp, resp.get("side", ""))

    def fetch_open_orders(self) -> List[Order]:
        """
        GET /v1/order/open-orders  (authenticated)

        Query: { symbol, timestamp }
        """
        params = {"symbol": self._market_symbol}
        resp = self._request(
            "GET",
            "/v1/order/open-orders",
            params=params,
            authenticated=True,
        )

        if "error" in resp:
            logger.error(f"Failed to fetch open orders: {resp.get('message')}")
            return []

        data = resp if isinstance(resp, list) else resp.get("data", [])
        if not isinstance(data, list):
            return []

        orders: List[Order] = []
        for o in data:
            if isinstance(o, dict):
                parsed = self._parse_order_response(o, o.get("side", ""))
                if parsed:
                    orders.append(parsed)
        return orders

    def cancel_all_orders(self) -> bool:
        """
        DELETE /v1/order/cancel-all  (authenticated)

        Body: { symbol }
        """
        params = {"symbol": self._market_symbol}
        resp = self._request(
            "DELETE",
            "/v1/order/cancel-all",
            params=params,
            authenticated=True,
        )

        if "error" in resp:
            logger.error(f"Failed to cancel all orders: {resp.get('message')}")
            return False

        logger.info("All open orders cancelled")
        return True

    # =======================================================================
    #  RESPONSE PARSING
    # =======================================================================

    def _parse_order_response(self, resp: dict, side: str) -> Optional[Order]:
        """Map a SharkEx v1 JSON order response to our Order dataclass."""
        try:
            return Order(
                order_id=resp.get("clientOrderId", resp.get("id", "")),
                client_order_id=resp.get("clientOrderId", ""),
                symbol=resp.get("symbol", self._market_symbol),
                side=(resp.get("side", side) or "").upper(),
                order_type=resp.get("type", resp.get("orderType", "")),
                price=float(resp.get("price", 0) or 0),
                stop_price=float(resp.get("stopPrice", 0) or 0),
                quantity=float(resp.get("origQty", resp.get("quantity", 0)) or 0),
                executed_qty=float(resp.get("executedQty", 0) or 0),
                status=resp.get("status", "NEW"),
                avg_price=float(resp.get("avgPrice", 0) or 0),
                raw=resp,
            )
        except Exception as e:
            logger.error(f"Error parsing order response: {e} | raw={resp}")
            return None


# ---------------------------------------------------------------------------
#  Factory
# ---------------------------------------------------------------------------

def create_exchange_client(cfg: BotConfig) -> SharkExClient:
    """Create a SharkEx v1 exchange client from bot configuration."""
    return SharkExClient(
        api_key=cfg.exchange.api_key,
        api_secret=cfg.exchange.api_secret,
        symbol=cfg.exchange.symbol,
    )