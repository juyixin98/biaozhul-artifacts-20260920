"""基于 GF(2^8) 的 Shamir 秘密分享（秘密按字节逐元素分享）。

原理：取随机系数构造多项式 P(x) = s + a1 x + ... + a_{t-1} x^{t-1}，
秘密 s 即常数项；每个份额为点 (i, P(i))。任意 t 个不同的点可在 x=0 处
做拉格朗日插值恢复 s，少于 t 个点在信息论意义上得不到任何信息。

每个秘密字节独立进行上述运算，因此秘密长度任意，上限由调用方约束。
随机数全部来自 ``secrets``（操作系统 CSPRNG）。
"""

from dataclasses import dataclass
import secrets
from itertools import combinations

from .gf import GF256
from .errors import ParameterError, ThresholdError, DuplicateIndexError, ConsistencyError

MIN_SHARES = 2
MAX_SHARES = 255
# 当需要诊断的组合数超过该上限时，确定性等间隔抽选组合（覆盖头尾），
# 避免组合爆炸导致恢复接口被拖慢。
DIAGNOSE_COMBO_CAP = 128


@dataclass(frozen=True)
class SplitParams:
    """带版本语义的分享参数。"""

    threshold: int
    total: int

    def validate(self) -> None:
        if not isinstance(self.threshold, int) or not isinstance(self.total, int):
            raise ParameterError("threshold and total must be integers")
        if self.threshold < MIN_SHARES:
            raise ParameterError(f"threshold must be >= {MIN_SHARES}")
        if self.total < self.threshold:
            raise ParameterError("total shares must be >= threshold")
        if self.total > MAX_SHARES:
            raise ParameterError(f"total shares must be <= {MAX_SHARES} in GF(2^8) (x in 1..255)")


@dataclass(frozen=True)
class SharePoint:
    """一个恢复点：横坐标 x 与纵坐标（与秘密等长的字节串）。"""

    x: int
    y: bytes

    def __post_init__(self):
        if not 1 <= self.x <= MAX_SHARES:
            raise ParameterError(f"share index x must be in 1..{MAX_SHARES}, got {self.x}")


def split_secret(secret: bytes, params: SplitParams, rng=secrets) -> list:
    """生成 ``params.total`` 个份额点，横坐标固定取 1..total。"""
    params.validate()
    if not isinstance(secret, (bytes, bytearray)):
        raise ParameterError("secret must be bytes")
    if len(secret) == 0:
        raise ParameterError("secret must not be empty")

    t, n = params.threshold, params.total
    # 随机系数 c1..c_{t-1}，多项式 P(x) = s + c1 x + ... + c_{t-1} x^{t-1}
    coeffs = [bytes(rng.randbelow(256) for _ in range(len(secret))) for _ in range(t - 1)]

    points = []
    for x in range(1, n + 1):
        # Horner 从最高次系数开始：acc = c_{t-1};
        # acc = acc*x + c_i（GF(2^8) 中加即 XOR）；最后 acc = acc*x + s
        if coeffs:
            acc = bytes(coeffs[-1])
            for c in reversed(coeffs[:-1]):
                acc = bytes(b ^ d for b, d in zip(GF256.vec_mul_scalar(acc, x), c))
            acc = bytes(b ^ d for b, d in zip(GF256.vec_mul_scalar(acc, x), secret))
        else:
            acc = bytes(secret)
        points.append(SharePoint(x=x, y=acc))
    return points


def _lagrange_at_zero(points) -> bytes:
    """对给定点集在 x=0 处做拉格朗日插值（逐字节）。

    ``points`` 为 ``(x, y_bytes)`` 元组列表。
    """
    length = len(points[0][1])
    result = bytearray(length)
    for i, (xi, yi) in enumerate(points):
        num = 1
        den = 1
        for j, (xj, _) in enumerate(points):
            if i == j:
                continue
            num = GF256.mul(num, xj)            # (0 - xj) = xj（GF(2^8) 中负即自身）
            den = GF256.mul(den, xi ^ xj)      # (xi - xj) = xi XOR xj
        weight = GF256.div(num, den)
        scaled = GF256.vec_mul_scalar(yi, weight)
        for k, b in enumerate(scaled):
            result[k] ^= b
    return bytes(result)


def combine_points(points, threshold: int) -> bytes:
    """恢复秘密。

    - 少于阈值：抛 :class:`ThresholdError`（接口层据此拒绝恢复）；
    - 重复横坐标：抛 :class:`DuplicateIndexError`；
    - 纵坐标长度不一致：抛 :class:`ParameterError`。
    超过阈值时取横坐标最小的 ``threshold`` 个点（调用方应先做一致性诊断）。
    """
    if not isinstance(threshold, int) or threshold < 1:
        raise ParameterError("invalid threshold")
    xs = [p.x for p in points]
    if len(set(xs)) != len(xs):
        dupes = sorted({x for x in xs if xs.count(x) > 1})
        raise DuplicateIndexError(f"duplicate share index (x): {dupes}")
    if len(points) < threshold:
        raise ThresholdError(
            f"need at least {threshold} distinct shares, got {len(points)}"
        )
    lengths = {len(p.y) for p in points}
    if len(lengths) != 1:
        raise ParameterError(f"share payload length mismatch: {sorted(lengths)}")

    chosen = sorted(points, key=lambda p: p.x)[:threshold]
    return _lagrange_at_zero([(p.x, p.y) for p in chosen])


def _sample_combos(combos, cap: int):
    """等间隔确定性抽取至多 cap 个组合（始终包含第一个与最后一个）。"""
    combos = list(combos)
    if len(combos) <= cap:
        return combos
    step = (len(combos) - 1) / (cap - 1)
    picked = []
    seen = set()
    for i in range(cap):
        idx = round(i * step)
        if idx not in seen:
            seen.add(idx)
            picked.append(combos[idx])
    return picked


def diagnose_points(points, threshold: int):
    """对超过阈值的点集做“子集投票”一致性诊断。

    返回 ``(secret | None, suspect_indices, votes, exhaustive)``：
    - 所有被检查的 t 元子集都恢复出同一个秘密时返回该秘密，suspect 为空；
    - 出现分歧时按得票分组：少数票来源的点下标列入 suspect，返回 None；
    - 点集数量等于阈值时无法做交叉验证，直接返回插值结果（无法诊断）。

    安全说明：这是最佳努力的启发式检查，**不是**恶意份额的密码学识别手段。
    无认证（无 HMAC）时，攻击者若能让自己的 t 个恶意份额同时进入，
    任何子集投票都无法发现异常；要确定性识别恶意参与者，必须使用
    信封的 HMAC 认证（分享与恢复时提供独立 auth_key）或可验证秘密分享。
    """
    if len(points) < threshold:
        raise ThresholdError(
            f"need at least {threshold} distinct shares, got {len(points)}"
        )

    if len(points) == threshold:
        return _lagrange_at_zero([(p.x, p.y) for p in points]), [], {}, True

    all_combos = list(combinations(range(len(points)), threshold))
    combos = _sample_combos(all_combos, DIAGNOSE_COMBO_CAP)
    exhaustive = len(combos) == len(all_combos)

    # secret(hex) -> {"count": int, "members": set(点下标)}
    votes = {}
    for combo in combos:
        subset = [points[i] for i in combo]
        candidate = _lagrange_at_zero([(p.x, p.y) for p in subset])
        entry = votes.setdefault(candidate.hex(), {"count": 0, "members": set()})
        entry["count"] += 1
        entry["members"].update(combo)

    if len(votes) == 1:
        secret = bytes.fromhex(next(iter(votes)))
        return secret, [], {h: v["count"] for h, v in votes.items()}, exhaustive

    # 按得票排序。存在平票时无法判定多数，所有平票候选都不能采信。
    ordered = sorted(votes.items(), key=lambda kv: kv[1]["count"], reverse=True)
    top_count = ordered[0][1]["count"]
    top_hexes = {h for h, v in ordered if v["count"] == top_count}
    tally = {h: v["count"] for h, v in ordered}

    good_members = set()
    for h in top_hexes:
        good_members |= votes[h]["members"]
    if len(top_hexes) == 1:
        # 唯一多数票：从未出现在多数票组合里的点为可疑点
        suspect = sorted(i for i in range(len(points)) if i not in good_members)
        detail = ("shares do not interpolate to a single secret; "
                  "malicious or mismatched shares suspected "
                  "(indices are 0-based request positions)")
    else:
        # 平票：无法判断哪个候选为真，把未覆盖到任何最高票组合的点也列为可疑
        suspect = sorted(i for i in range(len(points)) if i not in good_members)
        detail = ("subset vote is tied: no majority reconstructed secret; "
                  "the set contains malicious/mismatched shares but the true "
                  "secret cannot be distinguished without authenticated shares")
    raise ConsistencyError(detail, suspect=suspect, votes=tally)
