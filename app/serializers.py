from __future__ import annotations

from sqlalchemy.orm import Session

from .models import JournalEntry


def entry_to_out(db: Session, entry: JournalEntry) -> dict:
    reverses_voucher_no = None
    if entry.reverses_entry_id is not None:
        original = db.get(JournalEntry, entry.reverses_entry_id)
        reverses_voucher_no = original.voucher_no if original else None

    line_outs = []
    for line in entry.lines:
        reservation_no = None
        if line.reservation_id is not None:
            reservation = line.reservation
            reservation_no = reservation.request_no if reservation is not None else None
        line_outs.append(
            {
                "line_no": line.line_no,
                "fund_code": line.fund_code,
                "department_code": line.department_code,
                "account_code": line.account_code,
                "debit_cents": line.debit_cents,
                "credit_cents": line.credit_cents,
                "reservation_request_no": reservation_no,
                "description": line.description,
            }
        )

    return {
        "id": entry.id,
        "voucher_no": entry.voucher_no,
        "entry_date": entry.entry_date,
        "period_code": entry.period_code,
        "description": entry.description,
        "is_reversal": entry.is_reversal,
        "reverses_voucher_no": reverses_voucher_no,
        "reversal_reason": entry.reversal_reason,
        "created_by": entry.created_by,
        "created_at": entry.created_at,
        "lines": line_outs,
    }
