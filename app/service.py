"""服务层：事件规范化、验签、入库、全量回放、版本化报告（哈希链 + 真实签名）。"""
from __future__ import annotations

import time
from typing import Any

from nacl.signing import SigningKey, VerifyKey

from . import crypto
from .config import Settings, settings as default_settings
from .engine import replay
from .numerics import parse_amount
from .repository import EventRepository

_AMOUNT_FIELDS = {"amount", "price", "tier1_m1", "tier2_m2"}


class ServiceError(ValueError):
    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


def _require(p: dict, key: str, etype: str) -> Any:
    if key not in p:
        raise ServiceError("bad_event", f"{etype} 事件缺少字段 {key}")
    return p[key]


def normalize_payload(etype: str, payload: dict) -> dict:
    """API 输入 payload（十进制字符串）-> 引擎 payload（WAD 整数）。严格校验。"""
    p = dict(payload)

    if etype == "rate_schedule":
        return {
            "rate_per_second": parse_amount(
                _require(p, "rate_per_second", etype), field="rate_per_second"
            ),
            "tier1_m1": parse_amount(_require(p, "tier1_m1", etype), field="tier1_m1"),
            "tier2_m2": parse_amount(p.get("tier2_m2", 0), field="tier2_m2"),
            "liq_bonus_num": int(p["liq_bonus_num"]),
            "liq_bonus_den": int(p["liq_bonus_den"]),
            "debt_symbol": str(p.get("debt_symbol", "DEBT")),
        }

    if etype == "open_position":
        return {"position_id": str(_require(p, "position_id", etype))}

    if etype in ("deposit", "withdraw", "borrow", "repay", "liquidate"):
        out: dict = {"position_id": str(_require(p, "position_id", etype))}
        if "amount" in p:
            out["amount"] = parse_amount(p["amount"])
        if "asset" in p:
            out["asset"] = str(p["asset"])
        if etype == "liquidate" and "liquidator" in p:
            out["liquidator"] = str(p["liquidator"])
        return out

    if etype == "price_update":
        return {
            "asset": str(_require(p, "asset", etype)),
            "price": parse_amount(_require(p, "price", etype)),
        }

    raise ServiceError("unknown_type", f"未知事件类型 {etype}")


def _validate_basic(ev: dict) -> None:
    if not isinstance(ev.get("event_id"), str) or not ev["event_id"]:
        raise ServiceError("bad_event", "event_id 必须为非空字符串")
    ts = ev.get("ts")
    if not isinstance(ts, int) or isinstance(ts, bool) or ts < 0:
        raise ServiceError("bad_event", "ts 必须为非负整数秒")
    if not isinstance(ev.get("payload"), dict):
        raise ServiceError("bad_event", "payload 必须为对象")
    # rate_schedule 的奖励参数校验（引擎也会校验，这里提前给出清晰 400）
    if ev["type"] == "rate_schedule":
        p = ev["payload"]
        try:
            bnum, bden = int(p["liq_bonus_num"]), int(p["liq_bonus_den"])
        except (KeyError, TypeError, ValueError):
            raise ServiceError("bad_event", "liq_bonus_num/den 必须为整数")
        if bden <= 0 or bnum < bden:
            raise ServiceError("bad_event", "清算奖励参数非法（需 den>0 且 num>=den）")

class ReplayService:
    def __init__(
        self,
        repo: EventRepository,
        settings: Settings = default_settings,
        verify_key: VerifyKey | None = None,
        signing_key: SigningKey | None = None,
    ) -> None:
        self.repo = repo
        self.settings = settings
        self.verify_key = verify_key
        self.signing_key = signing_key

    async def ingest_batch(self, raw_events: list[dict]) -> dict:
        """原子化批量提交：校验/验签 -> 检测迟到 -> 入库 -> 全量回放 -> 出报告。"""
        s = self.settings

        # 0) 基础校验 + 批内重复
        ids: list[str] = []
        for ev in raw_events:
            _validate_basic(ev)
            ids.append(ev["event_id"])
        if len(set(ids)) != len(ids):
            raise ServiceError("duplicate_in_batch", "批次内存在重复 event_id", 409)

        # 1) 库内重复
        existing = await self.repo.get_existing_ids(ids)
        if existing:
            raise ServiceError(
                "duplicate_event_id",
                f"event_id 已存在: {sorted(existing)[:5]}",
                409,
            )

        # 2) 验签（真实 Ed25519）
        if s.require_signatures:
            if self.verify_key is None:
                raise ServiceError("key_missing", "服务未配置验签公钥", 500)
            for ev in raw_events:
                if not ev.get("sig") or not crypto.verify_event(ev, self.verify_key):
                    raise ServiceError(
                        "bad_signature", f"事件 {ev['event_id']} 签名无效", 400
                    )

        # 3) 规范化金额（非法定点/缺字段 -> 400）
        normalized: list[dict] = []
        for ev in raw_events:
            try:
                payload = normalize_payload(ev["type"], ev["payload"])
            except ServiceError:
                raise
            except (ValueError, TypeError) as exc:
                raise ServiceError(
                    "bad_amount", f"事件 {ev['event_id']}: {exc}"
                ) from None
            normalized.append(
                {
                    "event_id": ev["event_id"],
                    "ts": ev["ts"],
                    "type": ev["type"],
                    "payload": payload,
                    "sig": ev.get("sig"),
                }
            )

        # 4) 迟到判定：本批任一事件早于已处理事件的最大时间 => 触发版本化重算
        prev_max = await self.repo.get_max_ts()
        batch_max = max(e["ts"] for e in normalized)
        late = prev_max is not None and batch_max < prev_max

        # 版本号 = 旧报告数 + 1；旧报告全部保留
        prior_versions = await self.repo.list_report_versions()
        new_version = len(prior_versions) + 1

        # 5) 入库（append-only）
        await self.repo.insert_events(normalized, late=late, replay_version=new_version)

        # 6) 全量回放（旧事件 + 新事件，按 (ts, seq) 稳定排序）
        all_events = await self.repo.list_events()
        result = replay(
            all_events,
            replay_version=new_version,
            seconds_per_year=s.seconds_per_year,
            price_staleness_seconds=s.price_staleness_seconds,
            max_repay_num=s.max_repay_fraction_num,
            max_repay_den=s.max_repay_fraction_den,
        )

        # 7) 组装报告 + 哈希链 + 服务器签名（真实 Ed25519）
        prev_digest = prior_versions[-1]["digest"] if prior_versions else None
        created_from_ts = max((e["ts"] for e in all_events), default=0)
        report = {
            "version": new_version,
            "prev_hash": prev_digest,
            "event_ids": [e["event_id"] for e in all_events],
            "created_from_ts": created_from_ts,
            "created_at": int(time.time()),
            "late_recompute": late,
            "result": result,
        }
        digest = crypto.digest_hex(report)
        if self.signing_key is None:
            raise ServiceError("key_missing", "服务未配置报告签名私钥", 500)
        signature = self.signing_key.sign(
            crypto.report_digest(report)
        ).signature.hex()

        version_saved = await self.repo.save_report(
            digest=digest,
            prev_digest=prev_digest,
            body=report,
            signature=signature,
            created_from_ts=created_from_ts,
        )
        assert version_saved == new_version
        return {
            "accepted": len(normalized),
            "replay_version": new_version,
            "report_id": version_saved,
            "late_detected": late,
            "digest": digest,
            "signature": signature,
        }

    async def latest_report(self) -> dict | None:
        return await self.repo.get_report(None)

    async def report(self, version: int) -> dict | None:
        return await self.repo.get_report(version)

    async def events(self) -> list[dict]:
        return await self.repo.list_events()
