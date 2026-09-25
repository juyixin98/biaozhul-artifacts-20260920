"""密钥文件、权限与日志不泄露测试。"""

import json
import logging
import os
import stat

from dms.crypto import CryptoProvider
from dms.logging_utils import SecretScrubFilter, get_logger, sensitive_scope, scrub


def test_key_file_is_0600(tmp_path):
    path = CryptoProvider.generate().save(tmp_path / "k.json")
    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o600


def test_key_roundtrip_and_version(tmp_path):
    cp = CryptoProvider.generate()
    path = cp.save(tmp_path / "k.json")
    data = json.loads(path.read_text())
    assert data["version"] == 1
    assert "master_key_b64" in data
    cp2 = CryptoProvider.load(path)
    token = cp2.encrypt("x")
    assert cp.decrypt(token) == "x"


def test_load_or_create_is_idempotent(tmp_path):
    path = tmp_path / "sub" / "k.json"
    cp1 = CryptoProvider.load_or_create(path)
    cp2 = CryptoProvider.load_or_create(path)
    assert cp1.encrypt("y") and cp2.decrypt(cp1.encrypt("y")) == "y"


def test_invalid_key_file_reported_without_dump(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{not json")
    try:
        CryptoProvider.load(path)
        assert False, "应当报错"
    except Exception as exc:
        assert "not json" not in str(exc) or "密钥" in str(exc)


def test_scrub_replaces_registered_secret():
    doc = {"user": {"phone": "13812345678", "note": "备注里有 13812345678"},
           "arr": [9001]}
    with sensitive_scope(doc):
        assert "13812345678" not in scrub("处理失败，值=13812345678")
        assert "9001" not in scrub("数字 9001 出现")
        assert scrub("不含敏感信息") == "不含敏感信息"


def test_logger_filter_never_emits_secrets(tmp_path):
    log_path = tmp_path / "run.log"
    logger = get_logger("dms.test.secret")
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.addFilter(SecretScrubFilter())
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)

    secret = "S3CR3T-VALUE-张"
    with sensitive_scope({"x": secret}):
        logger.warning("出错了 value=%s", secret)
        logger.warning("明文消息嵌入 S3CR3T-VALUE-张 字样")

    handler.flush()
    contents = log_path.read_text(encoding="utf-8")
    assert secret not in contents
    assert "REDACTED" in contents
