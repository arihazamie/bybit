"""
exchange/
=========
Package konektor ke Bitget Futures (USDT-M).

Struktur:
  exchange/bitget/market_data.py  — market list cache (Step 4, read-only, tanpa auth)
  exchange/bitget/retry.py        — retry decorator + klasifikasi error (Step 7)
  exchange/bitget/rest_client.py  — REST client lengkap: balance, market, leverage (Step 7)
  exchange/bitget/ws_client.py    — WebSocket realtime: watch_orders, watch_positions (Step 8)
"""

from exchange.bitget import (
    BalanceInfo,
    BitgetMarketCache,
    BitgetRestClient,
    CriticalError,
    MarketInfo,
    MarketMatch,
    TransientError,
    classify_exception,
    get_default_market_cache,
    get_rest_client,
    reset_rest_client,
    with_retry,
    wrap_exchange_error,
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
    # retry (Step 7)
    "CriticalError",
    "TransientError",
    "classify_exception",
    "with_retry",
    "wrap_exchange_error",
]
