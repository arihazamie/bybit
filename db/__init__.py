"""
db/ — Database layer untuk bitget-signal-bot.

Import utama yang dibutuhkan komponen lain:
    from db.database import init_db, get_db, check_db_health
    from db.crud.settings import get_setting, set_setting, is_bot_paused, ...
    from db.crud.signal_log import is_message_processed, create_signal_log, ...
    from db.crud.trades import create_trade, update_trade_status, ...       (step 6b)
    from db.crud.circuit_breaker import get_cb_state, update_cb_state, ... (step 6b)
    from db.crud.event_log import log_event, ...                           (step 6b)
"""

from db.database import init_db, get_db, check_db_health, set_db_path

__all__ = [
    "init_db",
    "get_db",
    "check_db_health",
    "set_db_path",
]
