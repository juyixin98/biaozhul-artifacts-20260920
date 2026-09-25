# CLI 会话真实记录（Python 3.12.3 / cryptography 41.0.7）

```bash
$ head -c 200003 /dev/urandom > /tmp/env-cli/secret.bin

$ python3 -m envelope --store /tmp/env-cli/store new-key
{
  "created_at": "2026-09-23T17:46:40Z",
  "kid": "mk_b444343fe4d50896eb7c0c19"
}

$ python3 -m envelope --store /tmp/env-cli/store encrypt /tmp/env-cli/secret.bin --chunk-size 65536
{
  "blob_path": "/tmp/env-cli/store/blobs/f60fc099f68f455fc8298381b86d2015.blob",
  "blocks": 4,
  "chunk_size": 65536,
  "file_id": "f60fc099f68f455fc8298381b86d2015",
  "meta_path": "/tmp/env-cli/store/meta/f60fc099f68f455fc8298381b86d2015.meta",
  "plaintext_size": 200003,
  "wrapped_by_kid": "mk_b444343fe4d50896eb7c0c19"
}

$ sha256sum blobs/f60fc099f68f455fc8298381b86d2015.blob   # 轮换前
98cb553d498e94b7f6a39d959ddd2bb9533fb65f09b4620b63bc5e1c275c0263  /tmp/env-cli/store/blobs/f60fc099f68f455fc8298381b86d2015.blob

$ python3 -m envelope --store ... rotate f60fc099f68f455fc8298381b86d2015
{
  "blob_bytes_changed": 0,
  "file_id": "f60fc099f68f455fc8298381b86d2015",
  "header_version": 2,
  "new_kid": "mk_dec2b8f71a734a1609cd7d12",
  "old_kid": "mk_b444343fe4d50896eb7c0c19"
}

$ sha256sum blobs/f60fc099f68f455fc8298381b86d2015.blob   # 轮换后（应相同）
98cb553d498e94b7f6a39d959ddd2bb9533fb65f09b4620b63bc5e1c275c0263  /tmp/env-cli/store/blobs/f60fc099f68f455fc8298381b86d2015.blob

$ python3 -m envelope --store ... decrypt f60fc099f68f455fc8298381b86d2015 /tmp/env-cli/recovered.bin
{
  "file_id": "f60fc099f68f455fc8298381b86d2015",
  "output": "/tmp/env-cli/recovered.bin"
}

$ cmp /tmp/env-cli/secret.bin /tmp/env-cli/recovered.bin && echo MATCH
MATCH

$ python3 -m envelope --store ... info f60fc099f68f455fc8298381b86d2015
{
  "algorithm": "AES-256-GCM",
  "blocks": 4,
  "chunk_size": 65536,
  "file_id": "f60fc099f68f455fc8298381b86d2015",
  "header_version": 2,
  "plaintext_size": 200003,
  "wrapped_by_kid": "mk_dec2b8f71a734a1609cd7d12",
  "wrapping_key_created_at": "2026-09-23T17:46:40Z"
}

$ python3 -m envelope --store ... list-keys
{
  "keys": [
    {
      "created_at": "2026-09-23T17:46:40Z",
      "is_latest": false,
      "kid": "mk_b444343fe4d50896eb7c0c19"
    },
    {
      "created_at": "2026-09-23T17:46:40Z",
      "is_latest": true,
      "kid": "mk_dec2b8f71a734a1609cd7d12"
    }
  ]
}

# 截断 blob（截掉尾部 100 字节）后再解密：预期失败
$ truncate -s $((size-100)) $BLOB && python3 -m envelope ... decrypt ...
错误：容器被截断：期望再读 3423 字节，实际只剩 3323 字节
exit=2

# 还原 blob 后解密仍成功
{
  "file_id": "f60fc099f68f455fc8298381b86d2015",
  "output": "/tmp/env-cli/recovered3.bin"
}
MATCH
```
