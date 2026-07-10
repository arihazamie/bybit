"""
bot/pipeline/wiring.py
========================
Daftarkan execute_fn dan conflict_fns dari pipeline ke inline handlers
(signal_confirm.py, conflict_confirm.py) yang dibangun di Step 18.

Dipanggil sekali dari main.py setelah pipeline dan bot sudah dibuat.
"""

from __future__ import annotations

from core.logging_setup import get_logger

logger = get_logger(__name__)


def wire_inline_handlers(pipeline) -> None:
    """
    Sambungkan pipeline.execute_signal ke:
      - signal_confirm.set_execute_fn()  (tombol "Eksekusi" sinyal ambigu)
      - conflict_confirm.set_conflict_fns()  (tombol Tambah/Replace/Cancel)
    """
    from bot.control_bot.inline.signal_confirm import set_execute_fn
    from bot.control_bot.inline.conflict_confirm import set_conflict_fns
    from bot.parser.ambiguity import SignalEvaluation
    from core.constants import PositionAction

    # ── execute_fn: dipanggil saat user klik "Eksekusi" untuk sinyal ambigu
    async def execute_fn(evaluation: SignalEvaluation) -> str:
        # Paksa parse_status = SUCCESS supaya pipeline mau eksekusi
        from core.constants import ParseStatus
        evaluation.parse_status = ParseStatus.SUCCESS
        result_text = await pipeline.execute_signal(evaluation, conflict_action=None)
        return result_text

    set_execute_fn(execute_fn)
    logger.info("[wiring] execute_fn terdaftar ke signal_confirm")

    # ── add_fn: dipanggil saat user klik "Tambah" (posisi tambahan)
    async def add_fn(new_signal_data: dict, existing_trade: dict) -> str:
        evaluation = _rebuild_evaluation(new_signal_data)
        if evaluation is None:
            return "❌ Data sinyal tidak valid untuk tambah posisi."
        return await pipeline.execute_signal(
            evaluation, conflict_action=PositionAction.ADD
        )

    # ── replace_fn: dipanggil saat user klik "Replace"
    async def replace_fn(new_signal_data: dict, existing_trade: dict) -> str:
        evaluation = _rebuild_evaluation(new_signal_data)
        if evaluation is None:
            return "❌ Data sinyal tidak valid untuk replace posisi."
        return await pipeline.execute_signal(
            evaluation, conflict_action=PositionAction.REPLACE
        )

    # ── cancel_fn: dipanggil saat user klik "Cancel pending"
    async def cancel_fn(pair: str, existing_trade: dict) -> str:
        trade_id = existing_trade.get("id")
        if trade_id is None:
            return f"❌ trade_id tidak ditemukan untuk cancel pending {pair}."
        from bot.executor.order_manager import cancel_pending_order
        from exchange.bitget.rest_client import get_rest_client
        result = await cancel_pending_order(
            trade_id=trade_id, pair=pair, rest_client=get_rest_client()
        )
        from bot.executor.order_manager import format_order_management_notification
        return format_order_management_notification(result)

    set_conflict_fns(add_fn=add_fn, replace_fn=replace_fn, cancel_fn=cancel_fn)
    logger.info("[wiring] conflict_fns terdaftar ke conflict_confirm")


def _rebuild_evaluation(new_signal_data: dict):
    """
    Rekonstruksi SignalEvaluation minimal dari dict new_signal_data
    yang disimpan di pending_store saat conflict_confirm.
    """
    # Jika pipeline sudah attach evaluation asli ke dict, pakai itu
    ev = new_signal_data.get("_evaluation")
    if ev is not None:
        from core.constants import ParseStatus
        ev.parse_status = ParseStatus.SUCCESS
        return ev

    # Fallback: bangun ParsedSignal & SignalEvaluation dari fields
    try:
        from bot.parser.signal_parser import ParsedSignal
        from bot.parser.ambiguity import SignalEvaluation
        from core.constants import ParseStatus, MessageType

        parsed = ParsedSignal(
            raw_text="(rebuilt from conflict confirm)",
            direction=new_signal_data.get("direction"),
            pair_normalized=new_signal_data.get("pair"),
            pair_raw=new_signal_data.get("pair"),
            entry_type=new_signal_data.get("entry_type"),
            entry_price=new_signal_data.get("entry_price"),
            stop_loss=new_signal_data.get("sl_price"),
            symbol_valid=True,
        )
        return SignalEvaluation(
            raw_text=parsed.raw_text,
            message_type=MessageType.NEW_SIGNAL_CANDIDATE,
            parse_status=ParseStatus.SUCCESS,
            parsed=parsed,
        )
    except Exception:
        return None
