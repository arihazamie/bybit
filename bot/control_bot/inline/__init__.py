"""bot/control_bot/inline — TTL pending store & confirmation flows (step 18)."""

# NOTE: `pending_store` di sini adalah INSTANCE singleton PendingStore, bukan
# module `pending_store.py`. Import `from bot.control_bot.inline import
# pending_store` akan mengembalikan instance ini (shadow submodule).
# Kalau butuh akses module pending_store.py langsung (mis. untuk patch/mock
# di test), import eksplisit dari path submodule:
#   import bot.control_bot.inline.pending_store as pending_store_module
from bot.control_bot.inline.pending_store import make_pending_key, pending_store
from bot.control_bot.inline.signal_confirm import (
    send_ambiguous_confirm,
    set_execute_fn,
    handle_signal_callback,
)
from bot.control_bot.inline.conflict_confirm import (
    send_conflict_confirm,
    set_conflict_fns,
    handle_conflict_callback,
)

__all__ = [
    "pending_store",
    "make_pending_key",
    "send_ambiguous_confirm",
    "set_execute_fn",
    "handle_signal_callback",
    "send_conflict_confirm",
    "set_conflict_fns",
    "handle_conflict_callback",
]