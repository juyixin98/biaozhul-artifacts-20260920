# 实测记录（RUNLOG）

日期：2026-09-24
环境：Python 3.12.3 / Linux 6.8.0-90-generic / FastAPI 0.141.1 /
cryptography 50.0.1 / uvicorn 0.53.0 / pytest 9.1.1

本文件如实记录实际运行结果，不经过修饰。

## 1. 锁定依赖可复现性

* `pip install -r requirements-lock.txt` 初版失败：本机原 venv 里的
  `sniffio==1.3.2` 在公开索引不存在（最高 1.3.1）。
* 处理：在**全新 venv** 中按直接依赖重新解析、`pip freeze` 生成当前
  `requirements-lock.txt`（新版 httpx/anyio 已不再带 sniffio/httpcore）。
* 复测：干净 venv 安装成功，`import fastapi, uvicorn, cryptography, pytest,
  httpx` 正常，`pytest` 28/28 通过。

## 2. 自动化测试

命令：`.venv/bin/python -m pytest -q`

结果：**28 passed, 1 warning in 0.6s**（warning 为 FastAPI TestClient 的
anyio 兜底提示，与本项目代码无关；已将弃用的 `@app.on_event` 改为 lifespan）。

覆盖矩阵（均为实测通过）：

| 场景 | 判定 |
|---|---|
| 空日志 + seq=0 创世锚 | VALID |
| 5 条记录、锚覆盖链头 | VALID |
| 重启后从 JSONL 重载并验证 | VALID |
| 改 payload（seq3） | TAMPERED（自哈希不符） |
| 改 actor 并重算自哈希 | TAMPERED（prev_hash 断裂/锚不一致） |
| 删除记录 | TAMPERED（缺号/断链） |
| 重排两条记录 | TAMPERED |
| 重排检查点 | TAMPERED（prev_checkpoint_hash 不符） |
| 翻转检查点签名字节 | TAMPERED（forged checkpoint） |
| 用他人私钥签检查点 | TAMPERED |
| 签名检查点与链上记录不一致 | TAMPERED |
| 删尾部记录+重算自哈希但无新签名 | TRUNCATED |
| 保留锚(seq6)但只导出 4 条 | TRUNCATED |
| 外部缓存锚(seq6)遇服务器回滚 | TAMPERED（他人密钥）/ TRUNCATED（同钥少记录） |
| 锚(seq4)后多一条干净尾部 | UNDETERMINED（proven_upto=4） |
| 干净尾部内篡改 | TAMPERED |
| 尾部补签新检查点 | 由 UNDETERMINED 转 VALID |
| 无任何锚的空导出 | UNDETERMINED |
| 区间 4..5，边界有 seq3 锚，--bounded-range | VALID |
| 区间 2..4，左边界无锚，--bounded-range | UNDETERMINED |
| 全量请求但区间止于 3、锚覆盖 6（expect_tail） | TRUNCATED |
| 用错误公钥验证 | TAMPERED（全部检查点验签失败） |

## 3. 真实 HTTP 端到端（uvicorn + curl + CLI 验证器）

`scripts/demo.sh` 对 127.0.0.1:8077 实跑，关键输出（实测）：

```
### 0. health    -> {"status":"ok","records":0,"checkpoints":1}   # 自动创世锚
### 1. append    -> seq 1/2/3
### 2. checkpoint -> seq=3
### 3. 尾部两条未签名 -> UNDETERMINED proven_upto=3
### 4. 补签 seq5 -> VALID proven_upto=5
### 5. 攻击矩阵
  modify covered record        -> TAMPERED     OK
  delete a record (gap)        -> TAMPERED     OK
  reorder two records          -> TAMPERED     OK
  truncate (3/5 records)       -> TRUNCATED    OK
  forge checkpoint signature   -> TAMPERED     OK
  rollback tail + its anchors  -> UNDETERMINED OK
### 6. 验证者 --remember 缓存 seq5 锚后，服务器只给 3 条
     -> TRUNCATED: trusted checkpoint covers seq=5
### 7. 区间导出 start=4&end=5 --bounded-range -> VALID
```

后台周期签名单独实测：`AUDIT_CHECKPOINT_INTERVAL=1` 启动，追加一条后约
1 秒内自动出现新检查点；链头未变化时的空转周期不再重复写检查点（已加去重）。

## 4. 复现步骤

```bash
python3 -m venv .venv && . .venv/bin/activate
python -m pip install -r requirements-lock.txt
python -m pytest -q                                   # 28 passed
# 终端 1：
AUDIT_DATA_DIR=./demo-data AUDIT_SIGNING_KEY=./demo-keys/server.pem \
  AUDIT_CHECKPOINT_INTERVAL=3600 \
  .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8077
# 终端 2：
scripts/demo.sh
```

## 5. 未完成 / 已知边界

* 无私钥泄露防护（无 HSM/KMS 集成、无密钥轮换/多签）——信任边界止于私钥保密。
* 记录明文落盘，不含加密/访问控制/鉴权（题目要求纯后端防篡改，未要求）。
* 单进程 `threading.Lock` + 本地 JSONL，未做高可用/多副本复制。
* 导出未分页（全量载入内存）；日志极大时需要流式导出，未实现。
* `GET /anchor/public-key` 仅为演示便利，生产必须走带外信任分发（README 已警告）。
* 时间戳由服务器时钟给出，攻击者持私钥场景下时间不可独立证明（顺序由 seq/链保证）。
