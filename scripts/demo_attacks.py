#!/usr/bin/env python3
"""端到端攻击 / 故障演示脚本（可直接运行，不需要 pytest）。

用法::

    python3 scripts/demo_attacks.py [存储目录]

用全新的存储目录依次演示：
  1. 正常加解密往返；
  2. 轮换主密钥：blob 哈希前后不变（只重包 DEK）；
  3. 篡改一个密文块字节 → AEAD 认证失败；
  4. 同文件内交换两个块 → nonce/AAD 校验失败；
  5. 跨文件交换块 → 文件 ID / DEK 绑定失败；
  6. 截断（删整帧 / 切半帧）→ 块数不一致 / 截断错误；
  7. 轮换在"原子替换前崩溃"→ 旧信封仍可用，重启后重试成功。
"""

from __future__ import annotations

import hashlib
import io
import os
import struct
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from envelope.errors import EnvelopeError  # noqa: E402
from envelope.service import EnvelopeService  # noqa: E402

_FRAME = struct.Struct(">I")


def parse_frames(blob: bytes) -> list[tuple[int, int]]:
    """返回每帧的 (起始偏移, 帧载荷长度)。"""
    assert blob[:5] == b"BLOB1"
    pos, frames = 5, []
    while pos < len(blob):
        (length,) = _FRAME.unpack(blob[pos : pos + 4])
        frames.append((pos, length))
        pos += 4 + length
    return frames


def expect_reject(title: str, fn) -> bool:
    try:
        fn()
    except EnvelopeError as exc:
        print(f"  [拒绝] {title}")
        print(f"         -> {type(exc).__name__}: {exc}")
        return True
    print(f"  [!!严重!!] {title}：攻击未被检测到！")
    return False


def decrypt(service, fid) -> bytes:
    out = io.BytesIO()
    service.decrypt_stream(fid, out)
    return out.getvalue()


def main(store: str | None = None) -> int:
    store = store or tempfile.mkdtemp(prefix="envelope-demo-")
    svc = EnvelopeService(store)
    mk1 = svc.create_master_key()
    print(f"存储目录   : {store}")
    print(f"主密钥 mk1 : {mk1.kid}")

    data = bytes((i * 31 + 7) % 256 for i in range(100_000))
    loc = svc.encrypt_stream(io.BytesIO(data), chunk_size=4096)
    nblocks = svc.describe(loc.file_id)["blocks"]
    assert decrypt(svc, loc.file_id) == data
    print(f"[1] 加解密往返 OK（{nblocks} 块，fid={loc.file_id[:16]}…）")

    # [2] 轮换：blob 哈希不变
    h_before = hashlib.sha256(loc.blob_path.read_bytes()).hexdigest()
    info = svc.rotate_master_key(loc.file_id)
    h_after = hashlib.sha256(loc.blob_path.read_bytes()).hexdigest()
    assert h_before == h_after and info.blob_bytes_changed == 0
    assert decrypt(svc, loc.file_id) == data
    print(f"[2] 轮换 OK: {info.old_kid[:12]}… -> {info.new_kid[:12]}…；"
          f"blob sha256 不变 {h_before[:16]}…；轮换后解密一致")

    good_blob = loc.blob_path.read_bytes()
    frames = parse_frames(good_blob)
    ok = True

    # [3] 篡改密文块
    raw = bytearray(good_blob)
    off, _ln = frames[2]
    raw[off + 4 + 12 + 100] ^= 0xFF
    loc.blob_path.write_bytes(bytes(raw))
    ok &= expect_reject("[3] 篡改第 3 块密文 1 字节", lambda: decrypt(svc, loc.file_id))
    loc.blob_path.write_bytes(good_blob)

    # [4] 同文件交换块（整帧对调，nonce 携带原序号）
    raw = bytearray(good_blob)
    (o0, l0), (o3, l3) = frames[0], frames[3]
    frame0 = bytes(raw[o0 : o0 + 4 + l0])
    frame3 = bytes(raw[o3 : o3 + 4 + l3])
    raw[o3 : o3 + 4 + l3] = frame0
    raw[o0 : o0 + 4 + l0] = frame3
    loc.blob_path.write_bytes(bytes(raw))
    ok &= expect_reject("[4] 同文件交换第 0/3 块", lambda: decrypt(svc, loc.file_id))
    loc.blob_path.write_bytes(good_blob)

    # [5] 跨文件搬块：新建第二个文件，把它的第 2 帧搬进 loc
    loc_b = svc.encrypt_stream(io.BytesIO(os.urandom(100_000)), chunk_size=4096)
    rb = loc_b.blob_path.read_bytes()
    fb = parse_frames(rb)
    raw = bytearray(good_blob)
    oa, la = frames[1]
    ob, lb = fb[1]
    assert la == lb
    raw[oa : oa + 4 + la] = rb[ob : ob + 4 + lb]
    loc.blob_path.write_bytes(bytes(raw))
    ok &= expect_reject("[5] 把另一文件的第 2 块搬入本文件", lambda: decrypt(svc, loc.file_id))
    loc.blob_path.write_bytes(good_blob)

    # [6] 截断
    loc.blob_path.write_bytes(good_blob[: frames[-2][0]])
    ok &= expect_reject("[6a] 删除尾部 2 个整块（截断）", lambda: decrypt(svc, loc.file_id))
    last_off, last_len = frames[-1]
    loc.blob_path.write_bytes(good_blob[: last_off + 4 + last_len // 2])
    ok &= expect_reject("[6b] 最后一块只写一半", lambda: decrypt(svc, loc.file_id))
    loc.blob_path.write_bytes(good_blob)
    assert decrypt(svc, loc.file_id) == data

    # [7] 轮换中断：在原子替换 meta 之前注入崩溃
    kid_before = svc.describe(loc.file_id)["wrapped_by_kid"]
    meta_before = loc.meta_path.read_bytes()

    class Kill(Exception):
        pass

    svc.crash_hook = lambda stage, fid: (_ for _ in ()).throw(Kill("模拟断电"))
    try:
        svc.rotate_master_key(loc.file_id)
        raise SystemExit("[!!严重!!] 未触发注入崩溃")
    except Kill:
        pass
    svc.crash_hook = None

    assert decrypt(svc, loc.file_id) == data
    assert loc.meta_path.read_bytes() == meta_before
    assert svc.describe(loc.file_id)["wrapped_by_kid"] == kid_before
    assert not list(loc.meta_path.parent.glob(".meta.*.tmp"))
    print("[7a] 轮换中断 OK：旧信封/旧密钥仍可解密，无临时文件残留")

    # 模拟"进程被杀"留下 tmp，重新构造服务（重启）后清扫并重试
    (loc.meta_path.parent / ".meta.leftover.tmp").write_bytes(b"partial")
    svc2 = EnvelopeService(store)
    assert not (loc.meta_path.parent / ".meta.leftover.tmp").exists()
    info2 = svc2.rotate_master_key(loc.file_id)
    assert decrypt(svc2, loc.file_id) == data
    assert loc.blob_path.read_bytes() == good_blob
    print(f"[7b] 重启重试 OK：清扫遗留 tmp，轮换到 {info2.new_kid[:12]}…，"
          "明文一致且 blob 未变")

    print()
    if ok:
        print("总结：全部篡改 / 交换 / 截断均被拒绝；轮换只重包 DEK，中断可安全恢复。")
    else:
        print("总结：存在未检测到的攻击！")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else None))
