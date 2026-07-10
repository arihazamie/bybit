"""
exchange/bitget
===============
Konektor Bitget Futures (USDT-M): market data (Step 4), REST (Step 7),
WebSocket realtime (Step 8), eksekusi order (Step 12-13).
"""

from exchange.bitget.market_data import (
    BitgetMarketCache,
    MarketMatch,
    get_default_market_cache,
)
from exchange.bitget.rest_client import (
    BalanceInfo,
    BitgetRestClient,
    MarketInfo,
    get_rest_client,
    reset_rest_client,
)
from exchange.bitget.retry import (
    CriticalError,
    TransientError,
    classify_exception,
    with_retry,
    wrap_exchange_error,
)
from exchange.bitget.ws_client import (
    BitgetWsClient,
    OrderEvent,
    PositionEvent,
    get_ws_client,
    reset_ws_client,
)

__all__ = [
    # market_data (Step 4)
    "BitgetMarketCache",
    "MarketMatch",
    "get_default_market_cache",
    # rest_client (Step 7)
    "BalanceInfo",
    "BitgetRestClient",
    "MarketInfo",
    "get_rest_client",
    "reset_rest_client",
    # retry
    "CriticalError",
    "TransientError",
    "classify_exception",
    "with_retry",
    "wrap_exchange_error",
    # ws_client (Step 8)
    "BitgetWsClient",
    "OrderEvent",
    "PositionEvent",
    "get_ws_client",
    "reset_ws_client",
]
