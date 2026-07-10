"""
db/crud/__init__.py
Re-export semua fungsi CRUD dari sub-modul.

Import dari sini untuk kemudahan:
    from db.crud import create_trade, log_event, get_cb_state, ...
"""

# ── Trades ──────────────────────────────────
from db.crud.trades import (
    create_trade,
    get_trade_by_id,
    get_trade_by_pair_and_status,
    get_open_trades,
    get_open_trade_for_pair,
    get_closed_trades,
    get_all_trades_by_pair,
    update_trade_status,
    close_trade,
    cancel_trade,
    update_trade_sl,
    update_trade_tp,
    update_trade_entry,
    update_trade_margin,
    update_trade_fields,
    get_daily_stats,
    get_open_trades_summary,
    count_open_trades,
    # async
    async_create_trade,
    async_get_trade_by_id,
    async_get_open_trades,
    async_get_open_trade_for_pair,
    async_get_closed_trades,
    async_update_trade_status,
    async_close_trade,
    async_update_trade_sl,
    async_update_trade_tp,
    async_update_trade_entry,
    async_update_trade_margin,
    async_get_daily_stats,
    async_get_open_trades_summary,
    async_cancel_trade,
)

# ── Settings ────────────────────────────────
from db.crud.settings import (
    get_setting,
    get_all_settings,
    set_setting,
    set_settings_batch,
    reset_setting_to_default,
    get_risk_mode,
    get_risk_amount_config,
    is_bot_paused,
    get_cb_thresholds,
    get_liquidation_buffer_pct,
    get_position_conflict_mode,
    set_bot_paused,
    # async
    async_get_setting,
    async_set_setting,
    async_get_all_settings,
    async_is_bot_paused,
    async_get_risk_mode,
    async_get_risk_amount_config,
    async_set_bot_paused,
    async_get_position_conflict_mode,
)

# ── Signal Log ──────────────────────────────
from db.crud.signal_log import (
    is_message_processed,
    create_signal_log,
    get_signal_log_by_id,
    get_signal_log_by_message_id,
    update_signal_action,
    get_signal_logs_awaiting_confirmation,
    count_signals_by_status,
    get_recent_signal_logs,
    # async
    async_is_message_processed,
    async_create_signal_log,
    async_update_signal_action,
    async_get_signal_logs_awaiting_confirmation,
)

# ── Circuit Breaker ──────────────────────────
from db.crud.circuit_breaker import (
    get_cb_state,
    get_all_cb_states,
    is_cb_open,
    is_any_cb_open,
    get_open_components,
    record_error,
    trip_circuit_breaker,
    transition_to_half_open,
    reset_circuit_breaker,
    reset_error_count,
    resume_all_components,
    get_cb_summary_for_dashboard,
    STATE_CLOSED,
    STATE_OPEN,
    STATE_HALF_OPEN,
    # async
    async_get_cb_state,
    async_get_all_cb_states,
    async_is_cb_open,
    async_is_any_cb_open,
    async_record_error,
    async_trip_circuit_breaker,
    async_transition_to_half_open,
    async_reset_circuit_breaker,
    async_reset_error_count,
    async_resume_all_components,
    async_get_cb_summary_for_dashboard,
)

# ── Event Log ───────────────────────────────
from db.crud.event_log import (
    log_event,
    log_circuit_breaker_trip,
    log_circuit_breaker_reset,
    log_leverage_adjusted,
    log_position_conflict,
    log_liquidation_warning,
    log_sl_hit,
    log_tp_hit,
    log_entry_filled,
    log_order_failed,
    log_bot_paused,
    log_bot_resumed,
    log_settings_changed,
    get_recent_events,
    get_events_by_severity,
    get_events_by_type,
    get_events_for_trade,
    get_critical_events_since,
    get_event_by_id,
    count_events_by_type_since,
    VALID_EVENT_TYPES,
    VALID_SEVERITIES,
    # async
    async_log_event,
    async_log_circuit_breaker_trip,
    async_log_circuit_breaker_reset,
    async_log_leverage_adjusted,
    async_log_position_conflict,
    async_log_liquidation_warning,
    async_log_sl_hit,
    async_log_tp_hit,
    async_log_entry_filled,
    async_log_order_failed,
    async_log_bot_paused,
    async_log_bot_resumed,
    async_log_settings_changed,
    async_get_recent_events,
    async_get_events_by_severity,
    async_get_events_for_trade,
    async_get_critical_events_since,
)
