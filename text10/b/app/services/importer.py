"""CSV 批量导入：按凭证号归组，整批一个事务，任何错误全部回滚。"""
import csv
import io
from collections import OrderedDict

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Account, AuditLog, Department, Fund, JournalEntry, JournalLine, Period
from app.services.posting import _error, _lock_open_period, validate_lines

MAX_ROWS = 2000

REQUIRED_COLUMNS = [
    "voucher_no", "period", "fund_code", "department_code",
    "account_code", "debit_cents", "credit_cents", "description",
]


def _parse_int(value: str, row_no: int, field: str) -> int:
    try:
        return int(value.strip())
    except (ValueError, AttributeError):
        raise _error(422, "BAD_NUMBER", f"第 {row_no} 行 {field} 不是有效整数: {value!r}")


def import_csv(db: Session, *, content: bytes, actor: str) -> list[int]:
    """导入 CSV，返回创建的凭证 id 列表。任何一行错误，整批回滚。"""
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise _error(422, "BAD_ENCODING", "CSV 必须是 UTF-8 编码")

    reader = csv.DictReader(io.StringIO(text))
    missing_cols = [c for c in REQUIRED_COLUMNS if c not in (reader.fieldnames or [])]
    if missing_cols:
        raise _error(422, "BAD_HEADER", f"CSV 缺少列: {missing_cols}")

    rows = list(reader)
    if not rows:
        raise _error(422, "EMPTY_FILE", "CSV 没有数据行")
    if len(rows) > MAX_ROWS:
        raise _error(422, "TOO_MANY_ROWS", f"CSV 最多 {MAX_ROWS} 行，实际 {len(rows)} 行")

    # 代码 -> id 映射
    funds = {f.code: f.id for f in db.scalars(select(Fund))}
    depts = {d.code: d.id for d in db.scalars(select(Department))}
    accounts = {a.code: a.id for a in db.scalars(select(Account))}
    periods = {(p.year, p.month): p.id for p in db.scalars(select(Period))}

    # 按凭证号归组（保持出现顺序）
    vouchers: "OrderedDict[str, dict]" = OrderedDict()
    for idx, row in enumerate(rows, start=2):  # 第 1 行是表头
        voucher_no = (row.get("voucher_no") or "").strip()
        if not voucher_no:
            raise _error(422, "MISSING_VOUCHER", f"第 {idx} 行缺少凭证号")
        period_str = (row.get("period") or "").strip()
        try:
            year_str, month_str = period_str.split("-")
            period_key = (int(year_str), int(month_str))
        except ValueError:
            raise _error(422, "BAD_PERIOD", f"第 {idx} 行期间格式应为 YYYY-MM: {period_str!r}")
        if period_key not in periods:
            raise _error(422, "UNKNOWN_PERIOD", f"第 {idx} 行期间不存在: {period_str}")
        fund_code = (row.get("fund_code") or "").strip()
        dept_code = (row.get("department_code") or "").strip()
        acct_code = (row.get("account_code") or "").strip()
        for code, mapping, label in (
            (fund_code, funds, "基金"), (dept_code, depts, "部门"), (acct_code, accounts, "科目"),
        ):
            if code not in mapping:
                raise _error(422, "UNKNOWN_CODE", f"第 {idx} 行未知{label}代码: {code!r}")

        line = {
            "fund_id": funds[fund_code],
            "department_id": depts[dept_code],
            "account_id": accounts[acct_code],
            "debit_cents": _parse_int(row.get("debit_cents") or "0", idx, "debit_cents"),
            "credit_cents": _parse_int(row.get("credit_cents") or "0", idx, "credit_cents"),
        }
        voucher = vouchers.setdefault(
            voucher_no,
            {"period_id": periods[period_key], "description": (row.get("description") or "").strip(), "lines": []},
        )
        if voucher["period_id"] != periods[period_key]:
            raise _error(422, "MIXED_PERIOD", f"凭证 {voucher_no} 跨多个期间")
        voucher["lines"].append(line)

    # 逐张校验并锁定期间（任一失败抛异常 -> 路由层回滚整批）
    locked_periods: set[int] = set()
    for voucher_no, voucher in vouchers.items():
        if voucher["period_id"] not in locked_periods:
            _lock_open_period(db, voucher["period_id"])
            locked_periods.add(voucher["period_id"])
        validate_lines(voucher["lines"])

    entry_ids: list[int] = []
    for voucher_no, voucher in vouchers.items():
        entry = JournalEntry(
            period_id=voucher["period_id"],
            description=voucher["description"] or f"导入凭证 {voucher_no}",
            status="posted",
            created_by=actor,
        )
        entry.lines = [JournalLine(**l) for l in voucher["lines"]]
        db.add(entry)
        db.flush()
        entry_ids.append(entry.id)

    db.add(AuditLog(actor=actor, action="import_csv", entity_type="journal_entry",
                    entity_id=",".join(map(str, entry_ids)),
                    detail=f"vouchers={len(vouchers)} rows={len(rows)}"))
    db.commit()
    return entry_ids
