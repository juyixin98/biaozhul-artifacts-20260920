"""业务逻辑：信任根初始化/轮换、制品登记、验签。

所有失败都抛出 :class:`ServiceError`（带 HTTP 状态码），由路由层统一转成响应。
"""

from __future__ import annotations

from typing import Any

from . import crypto
from .store import Store


class ServiceError(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


# --- 辅助 --------------------------------------------------------------------


def _build_key_map(public_keys: list[str], field: str) -> dict[str, str]:
    """把公钥 hex 列表转成 {key_id: 公钥hex}，顺带做长度/重复校验。"""

    result: dict[str, str] = {}
    for pub in public_keys:
        raw = crypto.unhex(pub, field)
        if len(raw) != 32:
            raise ServiceError(400, f"{field} 中的公钥必须为 32 字节（64 hex 字符）")
        kid = crypto.key_id(pub)
        if kid in result:
            raise ServiceError(400, f"{field} 列表中存在重复公钥 {kid}")
        result[kid] = pub.lower()
    return result


def _require_root(state: dict[str, Any]) -> dict[str, Any]:
    root = state.get("root")
    if root is None:
        raise ServiceError(409, "信任根尚未初始化，请先 POST /root/init")
    return root


# --- 信任根 ------------------------------------------------------------------


def init_root(store: Store, req: Any) -> dict[str, Any]:
    def _do(state: dict[str, Any]) -> dict[str, Any]:
        if state["root"] is not None:
            raise ServiceError(409, "信任根已存在，轮换请使用 POST /root/rotate")
        threshold_keys = _build_key_map(req.threshold_public_keys, "threshold_public_keys")
        signer_keys = _build_key_map(req.signer_public_keys, "signer_public_keys")
        if req.threshold > len(threshold_keys):
            raise ServiceError(400, "threshold 不能超过阈值公钥数量")

        root = {
            "root_version": req.root_version,
            "threshold": req.threshold,
            "threshold_keys": threshold_keys,
            "signer_keys": signer_keys,
        }
        state["root"] = root
        state["root_history"].append(root)
        return root

    return store.mutate(_do)


def rotate_root(store: Store, req: Any) -> dict[str, Any]:
    def _do(state: dict[str, Any]) -> dict[str, Any]:
        old_root = _require_root(state)

        # 根版本必须严格递增 —— 拒绝根版本回退。
        if req.new_root_version <= old_root["root_version"]:
            raise ServiceError(
                409,
                f"拒绝根版本回退：新版本 {req.new_root_version} 必须严格大于当前版本 "
                f"{old_root['root_version']}",
            )

        new_threshold_keys = _build_key_map(
            req.new_threshold_public_keys, "new_threshold_public_keys"
        )
        new_signer_keys = _build_key_map(
            req.new_signer_public_keys, "new_signer_public_keys"
        )
        if req.new_threshold > len(new_threshold_keys):
            raise ServiceError(400, "new_threshold 不能超过新阈值公钥数量")

        # 校验并统计「旧根」阈值成员的批准签名。
        approved_by: set[str] = set()
        for idx, approval in enumerate(req.approvals):
            kid = approval.get("key_id")
            sig = approval.get("signature")
            if not kid or not sig:
                raise ServiceError(400, f"approvals[{idx}] 必须同时包含 key_id 与 signature")
            if kid in approved_by:
                raise ServiceError(400, f"approvals[{idx}]：同一成员重复批准 {kid}")
            old_pub = old_root["threshold_keys"].get(kid)
            if old_pub is None:
                # 失效/不属于旧根的密钥无权批准。
                raise ServiceError(403, f"approvals[{idx}]：{kid} 不是当前信任根的阈值成员")
            ok = crypto.verify_root_rotation_approval(
                public_key_hex=old_pub,
                signature_hex=sig,
                new_root_version=req.new_root_version,
                new_threshold=req.new_threshold,
                new_threshold_keys=req.new_threshold_public_keys,
                new_signer_keys=req.new_signer_public_keys,
            )
            if not ok:
                raise ServiceError(403, f"approvals[{idx}]：批准签名无效（内容被改或签名错误）")
            approved_by.add(kid)

        if len(approved_by) < old_root["threshold"]:
            raise ServiceError(
                403,
                f"批准不足：需要至少 {old_root['threshold']} 个旧根成员，实际有效 {len(approved_by)} 个",
            )

        new_root = {
            "root_version": req.new_root_version,
            "threshold": req.new_threshold,
            "threshold_keys": new_threshold_keys,
            "signer_keys": new_signer_keys,
        }
        state["root"] = new_root
        state["root_history"].append(new_root)
        return new_root

    return store.mutate(_do)


def get_root(store: Store) -> dict[str, Any]:
    return _require_root(store.snapshot())


# --- 制品登记 ----------------------------------------------------------------


def register_artifact(store: Store, req: Any) -> dict[str, Any]:
    def _do(state: dict[str, Any]) -> dict[str, Any]:
        root = _require_root(state)

        # 输入格式校验（摘要 / nonce / 签名 / 版本）。
        try:
            digest_raw = crypto.unhex(req.digest, "digest")
            if len(digest_raw) != 32:
                raise ValueError("digest 长度")
            nonce_raw = crypto.unhex(req.nonce, "nonce")
            if len(nonce_raw) != 16:
                raise ValueError("nonce 长度")
            crypto.unhex(req.signature, "signature")
            crypto.parse_version(req.version)
        except (crypto.CryptoError, ValueError) as exc:
            raise ServiceError(400, f"请求字段不合法：{exc}") from exc

        signer_pub = root["signer_keys"].get(req.key_id)
        if signer_pub is None:
            raise ServiceError(403, f"key_id {req.key_id} 不在当前信任根的授权签名者中")

        highest = state["highest_versions"].get(req.artifact_type)
        if crypto.is_rollback(req.version, highest):
            raise ServiceError(
                409,
                f"拒绝版本回退/重复登记：{req.artifact_type} 已见最高版本 {highest}，"
                f"收到 {req.version}（版本必须严格递增）",
            )

        artifact_key = f"{req.artifact_type}@{req.version}"
        if artifact_key in state["artifacts"]:
            raise ServiceError(409, f"制品 {artifact_key} 已登记，禁止重复签名登记")

        if req.nonce in state["used_nonces"]:
            raise ServiceError(409, "重复签名被拒绝：该 nonce 已被消费（防重放）")

        # 签名绑定 (摘要, 类型, 版本, nonce)；任一字段对不上都验不过。
        sig_ok = crypto.verify_artifact_signature(
            public_key_hex=signer_pub,
            signature_hex=req.signature,
            digest_hex=req.digest,
            artifact_type=req.artifact_type,
            version=req.version,
            nonce_hex=req.nonce,
        )
        if not sig_ok:
            raise ServiceError(400, "签名验证失败：签名与(摘要/类型/版本/nonce)不匹配")

        record = {
            "artifact_type": req.artifact_type,
            "version": req.version,
            "digest": req.digest.lower(),
            "key_id": req.key_id,
            "public_key": signer_pub,
            "nonce": req.nonce.lower(),
            "signature": req.signature.lower(),
            "registered_at_root_version": root["root_version"],
        }
        state["artifacts"][artifact_key] = record
        state["highest_versions"][req.artifact_type] = req.version
        state["used_nonces"][req.nonce.lower()] = 1
        return record

    return store.mutate(_do)


# --- 验签 --------------------------------------------------------------------


def _invalid(reason: str, req: Any, kid: str | None, trusted: bool | None) -> dict[str, Any]:
    return {
        "valid": False,
        "reason": reason,
        "artifact_type": req.artifact_type,
        "version": req.version,
        "key_id": kid,
        "signer_trusted": trusted,
    }


def verify_artifact(store: Store, req: Any) -> dict[str, Any]:
    state = store.snapshot()
    root = _require_root(state)

    try:
        crypto.parse_version(req.version)
    except crypto.CryptoError as exc:
        raise ServiceError(400, str(exc)) from exc

    if req.public_key is not None:
        # —— 无状态验签：调用方自带公钥，服务端校验密码学有效性 + 是否受当前根信任 ——
        try:
            kid = crypto.key_id(req.public_key)
        except crypto.CryptoError as exc:
            raise ServiceError(400, str(exc)) from exc
        if req.key_id is not None and req.key_id != kid:
            return _invalid("提供的 key_id 与公钥不匹配", req, req.key_id, None)

        trusted = kid in root["signer_keys"]
        try:
            ok = crypto.verify_artifact_signature(
                public_key_hex=req.public_key,
                signature_hex=req.signature,
                digest_hex=req.digest,
                artifact_type=req.artifact_type,
                version=req.version,
                nonce_hex=req.nonce,
            )
        except crypto.CryptoError as exc:
            raise ServiceError(400, str(exc)) from exc
        if not ok:
            # 正文篡改导致摘要变化、跨类型/跨版本搬用签名都会落到这里。
            return _invalid(
                "签名无效：摘要/类型/版本/nonce 与签名不匹配（正文可能被篡改或签名被搬用）",
                req,
                kid,
                trusted,
            )
        if not trusted:
            return _invalid(
                "签名密码学有效，但签名者不在当前信任根中（密钥可能已随轮换失效）",
                req,
                kid,
                False,
            )
        return {
            "valid": True,
            "reason": "验签通过",
            "artifact_type": req.artifact_type,
            "version": req.version,
            "key_id": kid,
            "signer_trusted": True,
        }

    # —— 有状态验签：按 (类型, 版本) 查已登记制品 ——
    record = state["artifacts"].get(f"{req.artifact_type}@{req.version}")
    if record is None:
        raise ServiceError(404, f"未找到已登记制品 {req.artifact_type}@{req.version}")

    # 用登记时的签名与 nonce，对「调用方本次提供的摘要」验签：
    # 正文一旦被篡改，摘要对不上，立即失效。
    ok = crypto.verify_artifact_signature(
        public_key_hex=record["public_key"],
        signature_hex=record["signature"],
        digest_hex=req.digest,
        artifact_type=req.artifact_type,
        version=req.version,
        nonce_hex=record["nonce"],
    )
    trusted = record["key_id"] in root["signer_keys"]
    if not ok:
        return _invalid(
            "登记签名与当前摘要不匹配（正文已被篡改）",
            req,
            record["key_id"],
            trusted,
        )
    if not trusted:
        return _invalid(
            "登记签名有效，但该签名者已不在轮换后的当前信任根中（旧根/旧密钥已失效）",
            req,
            record["key_id"],
            False,
        )
    return {
        "valid": True,
        "reason": "验签通过（基于登记签名）",
        "artifact_type": req.artifact_type,
        "version": req.version,
        "key_id": record["key_id"],
        "signer_trusted": True,
    }


def list_artifacts(store: Store) -> list[dict[str, Any]]:
    return list(store.snapshot()["artifacts"].values())
