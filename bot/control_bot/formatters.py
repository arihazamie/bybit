"""
bot/control_bot/formatters.py
==============================
Format data menjadi pesan Telegram yang rapi.
Semua fungsi return string siap kirim (Markdown V2 atau plain text).
Gunakan HTML parse_mode agar tidak perlu escape karakter.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import pytz

from config.settings import settings

_TZ = pytz.timezone(settings.DISPLAY_TIMEZONE)


def _local(iso_utc: Optional[str]) -> str:
    if not iso_utc:
        return "—"
    try:
        dt = datetime.fromisoformat(iso_utc.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        local = dt.astimezone(_TZ)
        return local.strftime("%d/%m %H:%M")
    except Exception:
        return iso_utc[:16]


def _pnl(val: Optional[float]) -> str:
    if val is None:
        return "—"
    sign = "+" if val >= 0 else ""
    return f"{sign}{val:.2f} USDT"


def _pnl_pct(val: Optional[float]) -> str:
    if val is None:
        return "—"
    sign = "+" if val >= 0 else ""
    return f"{sign}{val:.2f}%"


def _price(val: Optional[float]) -> str:
    if val is None:
        return "—"
    return f"{val:.6g}"


def _r(val: Optional[float]) -> str:
    if val is None:
        return ""
    sign = "+" if val >= 0 else ""
    return f" ({sign}{val:.1f}R)"


def _extract_live_pnl(
    live: Optional[dict],
    entry_price: Optional[float],
    position_size: Optional[float],
    margin_used: Optional[float],
    direction: str,
) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """
    Ambil (current_price, pnl_usd, pnl_pct) dari data posisi live exchange (raw
    ccxt position dict — hasil `fetch_positions`).

    Prioritas:
      1. Pakai field unrealized PnL & percentage langsung dari exchange (paling
         akurat, sudah memperhitungkan cara hitung Bitget sendiri).
      2. Kalau field itu kosong tapi markPrice/last tersedia, hitung manual
         dari entry_price & position_size sebagai fallback supaya angka tetap
         tampil walau response ccxt tidak lengkap.
    """
    if not live:
        return None, None, None

    current_price = live.get("markPrice") or live.get("last") or live.get("lastPrice")
    pnl_usd = live.get("unrealizedPnl")
    if pnl_usd is None:
        pnl_usd = live.get("unrealizedPnlAmount")
    pnl_pct = live.get("percentage")

    if current_price is not None:
        current_price = float(current_price)

    if pnl_usd is None and current_price is not None and entry_price and position_size:
        diff = current_price - entry_price
        if direction == "short":
            diff = -diff
        pnl_usd = diff * position_size

    if pnl_usd is not None:
        pnl_usd = float(pnl_usd)

    if pnl_pct is None and pnl_usd is not None and margin_used:
        pnl_pct = (pnl_usd / margin_used) * 100
    if pnl_pct is not None:
        pnl_pct = float(pnl_pct)

    return current_price, pnl_usd, pnl_pct


def fmt_dashboard(
    balance: Optional[object],
    summary: dict,
    daily_stats: dict,
    is_paused: bool,
) -> str:
    lines = ["<b>📊 Dashboard</b>"]

    # Balance
    if balance:
        lines += [
            "",
            f"<b>Balance</b>",
            f"  Equity      : <code>{balance.total_equity:,.2f} USDT</code>",
            f"  Free margin : <code>{balance.free_margin:,.2f} USDT</code>",
            f"  Wallet      : <code>{balance.wallet_balance:,.2f} USDT</code>",
            f"  Unrealized  : <code>{_pnl(balance.unrealized_pnl)}</code>",
        ]
    else:
        lines += ["", "Balance: <i>tidak tersedia (periksa koneksi exchange)</i>"]

    # Posisi
    lines += [
        "",
        f"<b>Posisi</b>",
        f"  Open    : {summary.get('open_count', 0)}",
        f"  Pending : {summary.get('pending_count', 0)}",
        f"  Margin  : <code>{summary.get('total_margin_used', 0):.2f} USDT</code>",
    ]
    pairs = summary.get("pairs_open", [])
    if pairs:
        lines.append(f"  Pairs   : {', '.join(pairs)}")

    # P&L hari ini
    total_pnl = daily_stats.get("total_pnl", 0)
    total_trades = daily_stats.get("total_trades", 0)
    wins = daily_stats.get("winning_trades", 0)
    lines += [
        "",
        f"<b>Hari ini ({daily_stats.get('date', '?')})</b>",
        f"  Trades  : {total_trades} ({wins} menang)",
        f"  P&amp;L    : <code>{_pnl(total_pnl)}</code>",
    ]

    lines += ["", f"Status bot: {'⏸ <b>PAUSED</b>' if is_paused else '▶️ <b>RUNNING</b>'}"]
    return "\n".join(lines)


def fmt_positions(trades: list[dict], live_positions: Optional[dict] = None) -> str:
    """
    Format daftar posisi open/pending.

    live_positions: dict opsional {symbol -> raw ccxt position dict} hasil
    `fetch_positions()` live dari exchange. Kalau tersedia (hanya berlaku
    untuk posisi status 'open'), tiap posisi ditampilkan lengkap dengan harga
    sekarang, floating P/L dalam USDT, dan P/L dalam persen — supaya laporan
    /positions setara dashboard exchange, bukan cuma data statis dari DB.
    """
    if not trades:
        return "Tidak ada posisi open atau pending saat ini."

    live_positions = live_positions or {}
    lines = [f"<b>📋 Posisi ({len(trades)})</b>"]

    for i, t in enumerate(trades, 1):
        direction = t["direction"]
        direction_icon = "🟢" if direction == "long" else "🔴"
        status_label = "⏳ pending" if t["status"] == "pending" else "✅ open"
        lev = f"{t['leverage_used']:.0f}x" if t.get("leverage_used") else "—"
        margin_used = t.get("margin_used")
        margin = f"{margin_used:.2f} USDT" if margin_used else "—"
        tp = f"{t['tp_price']:.6g}" if t.get("tp_price") else "—"
        opened = _local(t.get("opened_at") or t.get("created_at"))

        lines += [
            "",
            f"<b>{i}. {t['pair']} {direction_icon} {t['direction'].upper()}</b> | {status_label}",
            f"   Entry  : <code>{t['entry_price']:.6g}</code>  |  SL: <code>{t['sl_price']:.6g}</code>  |  TP: <code>{tp}</code>",
            f"   Size   : <code>{t['position_size']:.6g}</code>  |  Lev: {lev}  |  Margin: <code>{margin}</code>",
            f"   Risk   : <code>{t['risk_amount_usd']:.2f} USDT</code> ({t['risk_mode']})",
        ]

        if t["status"] == "open":
            live = live_positions.get(t["pair"])
            current_price, pnl_usd, pnl_pct = _extract_live_pnl(
                live,
                entry_price=t.get("entry_price"),
                position_size=t.get("position_size"),
                margin_used=margin_used,
                direction=direction,
            )
            pnl_icon = "📈" if (pnl_usd or 0) >= 0 else "📉"
            lines.append(
                f"   Harga  : <code>{_price(current_price)}</code>  |  "
                f"P/L: {pnl_icon} <code>{_pnl(pnl_usd)}</code> "
                f"(<code>{_pnl_pct(pnl_pct)}</code>)"
            )
            if live is None:
                lines.append("   ⚠️ Data live exchange tidak tersedia — P/L belum terupdate")

        lines.append(f"   Masuk  : {opened}")

        if t.get("leverage_auto_adjusted"):
            lines.append("   ⚠️ Leverage diturunkan otomatis (safety SL)")

    return "\n".join(lines)


def fmt_history(trades: list[dict]) -> str:
    if not trades:
        return "Belum ada trade yang ditutup."

    lines = [f"<b>📜 History ({len(trades)} trade terakhir)</b>"]

    for t in trades:
        direction_icon = "🟢" if t["direction"] == "long" else "🔴"
        reason_icons = {
            "sl_hit": "🛑", "tp_hit": "🎯",
            "manual_close": "👋", "liquidated": "💀",
        }
        reason_icon = reason_icons.get(t.get("close_reason", ""), "❓")
        pnl_str = _pnl(t.get("pnl"))
        r_str = _r(t.get("r_multiple"))
        closed = _local(t.get("closed_at"))

        lines += [
            "",
            (
                f"{direction_icon} <b>{t['pair']}</b> {t['direction'].upper()} "
                f"| {reason_icon} {t.get('close_reason', '?')} "
                f"| <code>{pnl_str}</code>{r_str} | {closed}"
            ),
        ]

    return "\n".join(lines)


def fmt_settings(all_settings: dict) -> str:
    risk_mode = all_settings.get("risk_mode", "percent")
    risk_pct = all_settings.get("risk_percent", "1.0")
    max_loss = all_settings.get("max_loss_usd", "5.0")
    paused = all_settings.get("bot_paused", "true").lower() == "true"
    auto_exec = all_settings.get("auto_execute_mode", "false").lower() == "true"
    conflict = all_settings.get("position_conflict_mode", "ask")
    liq_buf = all_settings.get("liquidation_buffer_pct", "5.0")
    cb_thr = all_settings.get("cb_error_threshold", "3")
    cb_win = all_settings.get("cb_window_minutes", "5")
    lev_cap = all_settings.get("default_leverage_cap") or "tidak ada (pakai max exchange)"

    if risk_mode == "percent":
        risk_line = f"<b>percent</b> — {risk_pct}% dari total balance"
    else:
        risk_line = f"<b>fixed_usd</b> — ${max_loss} per trade"

    return "\n".join([
        "<b>⚙️ Settings</b>",
        "",
        f"Risk mode       : {risk_line}",
        f"Max loss USD    : ${max_loss}",
        f"Bot status      : {'⏸ <b>PAUSED</b>' if paused else '▶️ <b>RUNNING</b>'}",
        f"Auto execute    : {'✅ on' if auto_exec else '❌ off'}",
        f"Conflict mode   : <code>{conflict}</code>",
        f"Liq. buffer     : {liq_buf}%",
        f"CB threshold    : {cb_thr} error dalam {cb_win} menit",
        f"Leverage cap    : {lev_cap}",
    ])


def fmt_status(cb_states: list[dict], db_health: dict, is_paused: bool) -> str:
    state_icons = {"closed": "✅", "open": "🔴", "half_open": "🟡"}
    lines = ["<b>🔧 System Status</b>", "", "<b>Circuit Breaker:</b>"]

    for cb in cb_states:
        icon = state_icons.get(cb["state"], "❓")
        errors = cb.get("consecutive_error_count", 0)
        last_err = cb.get("last_error_message") or ""
        err_suffix = f" | {last_err[:40]}" if last_err and cb["state"] != "closed" else ""
        lines.append(
            f"  {icon} <code>{cb['component']:<20}</code> {cb['state'].upper()}"
            f" ({errors} err){err_suffix}"
        )

    db_icon = "✅" if db_health.get("status") == "healthy" else "❌"
    db_detail = (
        f"{db_health.get('trade_count', 0)} trades, {db_health.get('signal_count', 0)} signals"
        if db_health.get("status") == "healthy"
        else db_health.get("error", "error")
    )
    lines += [
        "",
        f"<b>Database:</b> {db_icon} {db_detail}",
        f"<b>Bot:</b> {'⏸ PAUSED' if is_paused else '▶️ RUNNING'}",
    ]
    return "\n".join(lines)