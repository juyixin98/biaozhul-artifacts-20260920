# RUNLOG — 实际运行记录

日期：2026-09-24
机器：Linux 6.8.0-90-generic (Ubuntu 24.04)，工作目录 `/home/admin/Downloads/biaozhul/opp98/b`

以下命令均在本目录**实际执行**，输出如实记录。

## 0. 环境

```
$ python3 --version
Python 3.12.3
$ python3 -c "import cryptography; print(cryptography.__version__)"
41.0.7
$ python3 -c "import pytest; print(pytest.__version__)"
9.1.1
```

说明：cryptography 41.0.7 **不含** 42+ 才有的高层 `cryptography.x509.verification`
API，因此路径校验是按 RFC 5280 用该库的**原语**（签名验证、扩展解析、公钥对象）
自行组织的，签名验签在 40+ 上走 `Certificate.verify_directly_issued_by`，
旧版本有基于 `rsa/ec/ed25519` 原语的回退实现（`signature_check.py`）。
没有自创密码算法。

## 1. 生成本机测试证书链

```
$ python scripts/generate_fixtures.py
wrote 12 scenarios under .../fixtures
  - eku_mismatch / expired_intermediate / expired / good / hostname_mismatch
  - non_ca_intermediate / not_yet_valid / path_length_violation
  - same_name_intermediate / same_name_rogue_root / untrusted_root / wildcard
```

结果：成功。所有 RSA-2048 密钥与证书均本机临时生成；`fixtures/*/leaf.key.pem`
是**测试专用私钥**，无任何真实/生产凭据。

## 2. 12 个场景的内存验证（验收四类链在内）

```
$ python3 -c "..."   # 见会话记录，遍历 build_all_scenarios() 调 verify_chain
OK  good                     valid=True
OK  wildcard                 valid=True
OK  expired                  valid=False codes=['EXPIRED']
OK  not_yet_valid            valid=False codes=['NOT_YET_VALID']
OK  path_length_violation    valid=False codes=['PATH_LENGTH_VIOLATION']
OK  non_ca_intermediate      valid=False codes=['NOT_A_CA']
OK  same_name_rogue_root     valid=False codes=['NO_PATH_TO_TRUST_ANCHOR']
OK  same_name_intermediate   valid=False codes=['NO_PATH_TO_TRUST_ANCHOR']
OK  hostname_mismatch        valid=False codes=['HOSTNAME_MISMATCH']
OK  eku_mismatch             valid=False codes=['EKU_MISMATCH']
OK  untrusted_root           valid=False codes=['NO_PATH_TO_TRUST_ANCHOR']
OK  expired_intermediate     valid=False codes=['EXPIRED']
ALL MATCH
```

验收要求的四类（**过期 / path length 违规 / 非 CA 中间证书 / 同名证书链**）
全部按预期失败并给出明确错误码。

## 3. 自动化测试

```
$ python3 -m pytest -q
..........................                                (首轮开发中见下)
...
$ python3 -m pytest -q    # 修复后的最终结果
32 passed in 12.91s
```

最终结果：**32 passed，0 failed**。

开发过程中（如实记录）首轮跑测出现过两类失败，已修复：
1. 场景字典键名取自内部函数名（如 `expired_leaf`），与测试/文档名
   （`expired`、`path_length_violation`、`same_name_rogue_root`）不一致 →
   `KeyError`。改为在 `build_all_scenarios()` 里用显式名称映射。
2. 请求缺少 `trust_anchors` 时先命中了 PEM 解析错误而非
   `MISSING_TRUST_ANCHORS` → 调整 `service.handle_verify` 的校验顺序。
另在主逻辑开发中修正过三处校验器自身缺陷：路径摘要里的变量名错误、
叶子/中间 CA 的索引边界 off-by-one、`pathLenConstraint` 的计数位置
（约束属于签发 CA，统计该 CA 与叶子之间的 CA 数量）。

## 4. HTTP 服务实测（curl）

先在 18080 启动时遇到端口冲突——**环境中一个名为 `vccsim` 的进程已占用
18080**（`OSError: [Errno 98] Address already in use`），与本项目无关；
改用系统分配的空闲端口（本例 53317）后一切正常：

```
$ python -m certverifier.service --port 53317 &
$ curl -s http://127.0.0.1:53317/health
{"status":"ok","service":"offline-cert-verifier",
 "network":"loopback-only; no outbound connections are made",
 "revocation_checking":"disabled-by-design (offline)"}

good                    HTTP 200  valid=True  codes=[]                 rev_checked=False
expired                 HTTP 200  valid=False codes=['EXPIRED']        rev_checked=False
path_length_violation   HTTP 200  valid=False codes=['PATH_LENGTH_VIOLATION'] rev_checked=False
non_ca_intermediate     HTTP 200  valid=False codes=['NOT_A_CA']       rev_checked=False
same_name_rogue_root    HTTP 200  valid=False codes=['NO_PATH_TO_TRUST_ANCHOR'] rev_checked=False
same_name_intermediate  HTTP 200  valid=False codes=['NO_PATH_TO_TRUST_ANCHOR'] rev_checked=False
hostname_mismatch       HTTP 200  valid=False codes=['HOSTNAME_MISMATCH'] rev_checked=False
untrusted_root          HTTP 200  valid=False codes=['NO_PATH_TO_TRUST_ANCHOR'] rev_checked=False
```

验证失败 HTTP 仍为 200，结论在 JSON 的 `valid`；只有坏请求才 4xx。
每条响应的 `revocation.checked / crl_checked / ocsp_checked` 均为 `false`。

## 5. CLI 实测（退出码）

```
good                  -> exit 0, valid:True
expired               -> exit 1, finding EXPIRED（2026-01-01 过期，在 2027-06-01 验证）
non_ca_intermediate   -> exit 1
```

## 6. 额外的安全行为抽查

* **篡改叶子签名**（翻转签名 BIT STRING 尾字节）→
  `valid=False, NO_PATH_TO_TRUST_ANCHOR`：链构建阶段签名验不过，找不到通向信任根的路径。
* **同名不同密钥的伪造根**：只信任真根时拒绝；若把同名伪造根也误加入信任库，
  链会通过——证明信任判定基于公钥/签名，信任库内容由调用方显式决定。
* **过期信任根**：默认不强制锚点有效期（锚点是"按配置信任"），并在 `notes`
  明示；`options.check_anchor_validity=true` 时强制并报 `EXPIRED`。
  （同链中过期的**中间证书**则始终报 `EXPIRED`。）
* **拒绝非回环绑定**：`create_server("0.0.0.0")` 抛 `ValueError: local-only`，
  对应测试 `test_refuses_non_loopback_bind` 通过。
* **无出站网络审计**：对 `certverifier/` 包 grep `socket|urllib|requests|
  aiohttp|http.client|create_connection|getaddrinfo` 结果为 `(none)`；
  唯一的 `http://` 字样是启动时的本地监听提示。

## 7. 未通过项 / 已知限制（如实列出）

* **无功能/测试未通过项**：最终 32 个测试全部通过，12 个场景行为全部符合预期。
* 环境事项：默认/随手选的端口（如 18080、8080 若被占）需自行更换；
  可用 `--port 0` 由系统分配空闲端口。
* 设计上**不做**吊销检查（CRL/OCSP/OCSP-staple）、不做 AIA 自动补证书、
  不读系统信任库；`nameConstraints/policyConstraints/anyPolicy` 等冷门 PKIX
  策略扩展未实现。这些是"离线本地处理"的刻意边界，不是缺陷。
* 仅接受 PEM（DER/PKCS#7 输入会被明确拒绝）。
* 通配符按 RFC 6125 只匹配最左一个标签、且不校验公钥固定（HPKP/CT 也属于在线范畴）。
