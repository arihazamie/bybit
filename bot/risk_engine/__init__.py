"""
bot/risk_engine
================
Step 9 — Risk & margin engine (lihat bagian 4 prompt.md).

Public API:
    calculate_trade_risk(...)   — orchestrator async (I/O: balance, leverage, ticker)
    RiskCalculationResult       — hasil kalkulasi (success/failure + semua angka)
    format_risk_notification()  — teks notifikasi Telegram (max loss vs margin)

    Fungsi murni (tanpa I/O, untuk unit test/komponen lain):
        calculate_risk_amount, calculate_sl_distance,
        calculate_position_size, calculate_margin_needed,
        resolve_leverage_used
"""

from bot.risk_engine.risk_engine import (
    RiskCalculationResult,
    calculate_margin_needed,
    calculate_position_size,
    calculate_risk_amount,
    calculate_sl_distance,
    calculate_trade_risk,
    format_risk_notification,
    resolve_leverage_used,
)

__all__ = [
    "RiskCalculationResult",
    "calculate_trade_risk",
    "calculate_risk_amount",
    "calculate_sl_distance",
    "calculate_position_size",
    "calculate_margin_needed",
    "resolve_leverage_used",
    "format_risk_notification",
]
