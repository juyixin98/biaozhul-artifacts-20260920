"""归档解包安全：全部恶意样例必须被拒绝，正常 zip/tar.gz 必须解出。"""

import io
import zipfile

import pytest

from app.archive import ArchiveError, extract_archive


def test_clean_zip_and_targz(examples):
    z = extract_archive((examples / "01-exact" / "sources.zip").read_bytes(), "sources.zip")
    t = extract_archive((examples / "01-exact" / "sources.tar.gz").read_bytes(), "sources.tar.gz")
    assert set(z) == {"src/Vault.sol", "src/SafeMath.sol"}
    assert z == t


@pytest.mark.parametrize(
    "name",
    ["zipslip.zip", "absolute.zip", "backslash.zip", "duplicate.zip", "nul.zip", "symlink.tar", "hardlink.tar"],
)
def test_every_malicious_archive_rejected(examples, name):
    path = examples / "_malicious" / name
    with pytest.raises(ArchiveError):
        extract_archive(path.read_bytes(), name)


def test_unknown_format_rejected():
    with pytest.raises(ArchiveError):
        extract_archive(b"this is not an archive at all" + b"\x00" * 64, "blob.bin")


def test_zip_bomb_limits():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        # 高压缩比：全零 1MiB，在 5MiB 总上限内，应正常解出
        zf.writestr("big.sol", b"\x00" * (1 * 1024 * 1024))
    out = extract_archive(buf.getvalue(), "big.zip")
    assert len(out["big.sol"]) == 1 * 1024 * 1024

    # 超过 5MiB 解压总量必须拒绝
    buf2 = io.BytesIO()
    with zipfile.ZipFile(buf2, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("huge.sol", b"\x00" * (6 * 1024 * 1024))
    with pytest.raises(ArchiveError):
        extract_archive(buf2.getvalue(), "huge.zip")


def test_too_many_entries_rejected():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for i in range(2049):
            zf.writestr(f"src/f{i}.sol", b"x")
    with pytest.raises(ArchiveError):
        extract_archive(buf.getvalue(), "many.zip")


def test_no_execution_only_bytes(examples):
    # 解包结果只是字节映射；服务从不 spawn/exec 任何内容
    out = extract_archive((examples / "01-exact" / "sources.zip").read_bytes(), "x.zip")
    assert all(isinstance(v, bytes) for v in out.values())
