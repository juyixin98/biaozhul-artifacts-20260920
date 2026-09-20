from __future__ import annotations

import csv
import io
from collections import OrderedDict

from ..config import settings
from ..errors import ValidationError
from ..schemas import EntryIn, LineIn
from .entries import create_entry

REQUIRED_COLUMNS = [
    "voucher_no",
    "entry_date",
    "period_code",
    "description",
    "line_no",
    "fund_code",
    "department_code",
    "account_code",
    "debit_cents",
    "credit_cents",
    "reservation_request_no",
    "line_description",
]


def _parse_int(value: str, *, row: int, field: str, errors: list[dict]) -> int:
    value = (value or "").strip()
    if value == "":
        return 0
    try:
        n = int(value)
    except ValueError:
        errors.append({"row": row, "voucher_no": None, "message": f"{field} is not an integer: {value!r}"})
        return 0
    if n < 0:
        errors.append({"row": row, "voucher_no": None, "message": f"{field} must be non-negative"})
    return n


def parse_csv(content: bytes) -> tuple[list[EntryIn], list[dict]]:
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        return [], [{"row": 0, "voucher_no": None, "message": f"File is not valid UTF-8: {exc}"}]

    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        return [], [{"row": 0, "voucher_no": None, "message": "CSV has no header row"}]
    missing = [c for c in REQUIRED_COLUMNS if c not in reader.fieldnames]
    if missing:
        return [], [
            {"row": 0, "voucher_no": None, "message": f"Missing columns: {', '.join(missing)}"}
        ]

    errors: list[dict] = []
    groups: "OrderedDict[str, dict]" = OrderedDict()
    row_count = 0

    for idx, raw in enumerate(reader, start=2):  # header is row 1
        row_count += 1
        if row_count > settings.max_csv_rows:
            errors.append(
                {
                    "row": idx,
                    "voucher_no": None,
                    "message": f"CSV exceeds maximum of {settings.max_csv_rows} data rows",
                }
            )
            break
        voucher_no = (raw.get("voucher_no") or "").strip()
        if not voucher_no:
            errors.append({"row": idx, "voucher_no": None, "message": "voucher_no is required"})
            continue
        if voucher_no not in groups:
            groups[voucher_no] = {
                "voucher_no": voucher_no,
                "entry_date": (raw.get("entry_date") or "").strip(),
                "period_code": (raw.get("period_code") or "").strip(),
                "description": (raw.get("description") or "").strip(),
                "rows": [],
                "first_row": idx,
            }
        else:
            head = groups[voucher_no]
            for field in ("entry_date", "period_code", "description"):
                val = (raw.get(field) or "").strip()
                if val and val != head[field]:
                    errors.append(
                        {
                            "row": idx,
                            "voucher_no": voucher_no,
                            "message": f"{field} conflicts with first row of voucher {voucher_no}",
                        }
                    )

        line_no = _parse_int(raw.get("line_no", ""), row=idx, field="line_no", errors=errors)
        debit = _parse_int(raw.get("debit_cents", ""), row=idx, field="debit_cents", errors=errors)
        credit = _parse_int(raw.get("credit_cents", ""), row=idx, field="credit_cents", errors=errors)
        reservation = (raw.get("reservation_request_no") or "").strip()
        groups[voucher_no]["rows"].append(
            {
                "row": idx,
                "line_no": line_no,
                "fund_code": (raw.get("fund_code") or "").strip(),
                "department_code": (raw.get("department_code") or "").strip(),
                "account_code": (raw.get("account_code") or "").strip(),
                "debit_cents": debit,
                "credit_cents": credit,
                "reservation_request_no": reservation or None,
                "description": (raw.get("line_description") or "").strip(),
            }
        )

    payloads: list[EntryIn] = []
    for voucher_no, g in groups.items():
        line_payloads = []
        seen_nos: set[int] = set()
        for r in g["rows"]:
            if r["line_no"] in seen_nos:
                errors.append(
                    {
                        "row": r["row"],
                        "voucher_no": voucher_no,
                        "message": f"Duplicate line_no {r['line_no']} within voucher",
                    }
                )
            seen_nos.add(r["line_no"])
            try:
                line_payloads.append(
                    LineIn(
                        line_no=r["line_no"],
                        fund_code=r["fund_code"],
                        department_code=r["department_code"],
                        account_code=r["account_code"],
                        debit_cents=r["debit_cents"],
                        credit_cents=r["credit_cents"],
                        reservation_request_no=r["reservation_request_no"],
                        description=r["description"],
                    )
                )
            except Exception as exc:  # pydantic validation -> batch error
                errors.append(
                    {
                        "row": r["row"],
                        "voucher_no": voucher_no,
                        "message": _first_pydantic_message(exc),
                    }
                )
        entry_date = _parse_date(
            g["entry_date"], g["first_row"], voucher_no, errors
        )
        if errors and any(e.get("voucher_no") == voucher_no for e in errors):
            # Skip assembly for this voucher; its rows already produced errors.
            continue
        try:
            payloads.append(
                EntryIn(
                    voucher_no=g["voucher_no"],
                    entry_date=entry_date,
                    period_code=g["period_code"],
                    description=g["description"],
                    lines=line_payloads,
                )
            )
        except Exception as exc:  # pydantic validation errors captured as batch errors
            errors.append(
                {
                    "row": g["first_row"],
                    "voucher_no": voucher_no,
                    "message": _first_pydantic_message(exc),
                }
            )

    return payloads, errors


def _first_pydantic_message(exc: Exception) -> str:
    errors = getattr(exc, "errors", None)
    if callable(errors):
        try:
            parts = []
            for err in errors():
                loc = ".".join(str(p) for p in err.get("loc", []) if p != "")
                parts.append(f"{loc}: {err.get('msg')}" if loc else err.get("msg", str(exc)))
            return "; ".join(parts) or str(exc)
        except Exception:
            pass
    return str(exc)


def _parse_date(value: str, row: int, voucher_no: str, errors: list[dict]):
    from datetime import date

    try:
        return date.fromisoformat(value)
    except ValueError:
        errors.append(
            {
                "row": row,
                "voucher_no": voucher_no,
                "message": f"entry_date {value!r} is not ISO YYYY-MM-DD",
            }
        )
        return None


def post_csv(db, content: bytes, actor: str):
    """
    Parse and post every voucher in one transaction. Any error rolls the
    entire batch back. Returns (entries, errors).
    """
    payloads, errors = parse_csv(content)
    if errors:
        raise ValidationError(
            f"{len(errors)} CSV error(s); entire batch rejected", details=errors
        )

    entries = []
    try:
        for payload in payloads:
            entries.append(create_entry(db, payload, actor=actor))
    except Exception:
        db.rollback()
        raise
    return entries
