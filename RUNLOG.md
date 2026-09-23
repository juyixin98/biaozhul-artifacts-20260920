# 运行记录（RUNLOG）

本文件如实记录本项目在交付环境中的实际执行命令与结果。所有命令均在
项目根目录 `/home/admin/Downloads/biaozhul/opp100/a` 下执行。

## 环境

```text
OS:      Linux 6.8.0-90-generic (Ubuntu)
Python:  Python 3.12.3
依赖:    cryptography 41.0.7（系统已安装；HMAC 原语来源）
pytest:  9.1.1
```

`pip install -r requirements.txt` 声明唯一第三方依赖 `cryptography>=41.0`；
HTTP 服务、base64、SHA256、随机数全部使用 Python 标准库。

## 1. 自动化测试

命令：

```bash
python3 -m pytest -v
```

结果（完整输出见交付时终端；摘要）：

```text
collected 83 items

tests/test_core.py      18 passed   # 拆分/恢复/混批/损坏/认证 等高层流程
tests/test_envelope.py  16 passed   # TSS1 信封往返、版本/域拒绝、SHA/HMAC
tests/test_gf.py        11 passed   # GF(2^8) FIPS 197 已知答案 + 域运算律
tests/test_service.py   19 passed   # 真实 HTTP socket 端到端
tests/test_shamir.py    19 passed   # 阈值恢复/拒绝/重复 x/诊断
============================== 83 passed in 9.27s ==============================
```

**未通过项：最终 0 项。** 开发过程中首轮测试曾有 51 项失败，
均为本方代码缺陷，已全部修复并复测通过（见第 4 节"开发中发现并修复的问题"）。

### 验收场景与测试用例对照

| 验收要求 | 代表用例 | 结果 |
|---|---|---|
| 任意阈值子集恢复 | `test_threshold2_all_pairs`、`test_threshold3_all_triples`（枚举**全部** t 元子集）、`test_recover_any_threshold_subset`、`test_split_and_recover_text/b64` | PASS |
| 少于阈值接口拒绝恢复 | `test_below_threshold_rejected/refused`、`test_recover_below_threshold`（HTTP 409 `below_threshold`） | PASS |
| 混批份额处理 | `test_mixed_batch_refused/mixed_batch`（split_id 不匹配 -> 409，`suspect` 标出外来份额） | PASS |
| 损坏编码处理 | `test_corrupted_base64_*`、`test_malformed_encoding_isolated`、`test_checksum_failure_*`、`test_all_bad_shares_bad` | PASS |
| 重复横坐标检测 | `test_duplicate_x_rejected/refused`、`test_recover_duplicate_x` | PASS |
| 参数/份额格式带版本 | `test_unknown_version_rejected`、`test_unknown_field_rejected`、`test_bad_magic`、`test_reserved_flag_rejected`、`test_length_field_tampering` | PASS |
| 成熟有限域 | `test_known_answer_vectors`（FIPS 197 §4.2：57·83=C1 等）、交换/结合/分配律、255 个非零元逆元 | PASS |
| 无认证不能识别恶意者（安全边界） | `test_checksum_does_not_stop_recomputing_attack`（攻击者重算 SHA256 后伪造**成功**，证明校验和只防意外）；`test_hmac_rejects_recomputed_sha_attack`（HMAC 下同样攻击**失败**）；`test_tampered_share_among_quorum_detected`（n=t+1 平票：拒绝但不指认） | PASS |

## 2. 随机化模糊测试（测试集之外的额外验证）

命令：一次性内联 Python 脚本（固定随机种子 20260924，可复现）。结果：

```text
[1] random subset recovery: 200/200 OK
    （随机 t∈[2,8]、n∈[t,t+6]、秘密 1..300 字节；每次随机取 k≥t 个份额）
[2] below-threshold refusal: 100/100
[3] accidental corruption refused/isolated: 50/50
    （随机翻载荷 1 字节、不重算标签：标签拒绝或隔离后阈值不足）
[4] recompute-checksum forgery, n=5/t=3: detected 100/100, located 100/100
    （攻击者翻位并重算 SHA256 绕过校验和；冗余充足时启发式定位坏份额）
[5] HMAC mode recovery: 50/50; HMAC forgery rejected at tag layer: 100/100
[6] extremes: n=255 OK; 1 MiB secret OK
ALL FUZZ CHECKS PASSED
```

## 3. 端到端实跑

### 3.1 HTTP 演示脚本

命令：

```bash
bash examples/http_demo.sh          # 自动申请内核空闲端口、起停服务
```

14 个场景全部按预期返回（实测输出摘录）：

- `GET /healthz` → 200 `{"status":"ok","service":"tss-local","version":1}`
- 3-of-5 拆分中文秘密 → 返回 5 个 `TSS1…` base64 信封、16 字节 split_id
- 取第 1/3/5 个份额恢复 → **200**，`secret_text` 与原秘密逐字一致
- 5 个全给（超阈值）→ **200**，`used_share_count=5`
- 只给 2 个（<3）→ **409 `below_threshold`**
- 同一份额重复 → **409 `duplicate_index`**，detail 标出请求位置 `[0,1]`
- 坏 base64 混入 + 3 好份额 → **200**，坏份额进 `rejected`（`malformed_share`），恢复成功
- 载荷翻位（SHA256 失配）+ 3 好份额 → **200**，`rejected` 记 `integrity_failure`，恢复成功
- 与另一批份额混传 → **409 `inconsistent_shares`**，`suspect:[2]`
- 恶意翻位**并重算 SHA256**（n=4,t=3）→ **409**，平票 4 个候选各 1 票；服务拒绝输出，
  detail 明确说明"无多数票、无认证份额时无法辨别真秘密"
- HMAC 模式拆分/正常恢复 → 200，`authenticated:true`
- HMAC 模式下伪造并重算 SHA256 → 伪造份额在标签层 HMAC 失败被拒，
  仅剩 2 个有效份额 < 3 → **409 `below_threshold`**，`rejected` 记 HMAC 失败原因
- `/validate` → 200，返回版本、域、t/n、x、split_id、载荷长度、认证标志

### 3.2 CLI 实跑

命令与实测结果：

```text
$ python3 -m tss.cli split --text "cli-secret-秘密" -t 2 -n 3 --json
  → split ok: 2 3 authenticated= False
$ head -2 shares | python3 -m tss.cli recover
cli-secret-秘密
$ head -1 shares | python3 -m tss.cli recover ; echo exit=$?
{"error":"below_threshold","detail":"1 valid share(s) < threshold 2; recovery refused"}
exit=1
$ KEY=$(python3 -m tss.cli gen-auth-key)     # 44 字符 base64 = 32 字节
$ python3 -m tss.cli split --text "mac" -t 2 -n 2 --auth-key-b64 "$KEY" …
$ python3 -m tss.cli recover --auth-key-b64 "$KEY" …
mac
$ head -1 shares | python3 -m tss.cli validate
{"version":1,"field":"GF(2^8)-Rijndael-0x11b","threshold":2,"total":3,"x":1, …}
```

## 4. 开发中发现并修复的问题（如实记录）

首轮 `pytest` 有 51 项失败，排查后确认是 4 个真实实现缺陷 + 若干测试预期问题，
均已修复，修复后 83/83 通过：

1. **GF 建表误用生成元**（密码学正确性问题，影响所有运算）：初版按"乘以 2"
   构造指数表，但在 Rijndael 域中 **2 的乘法阶只有 51，不是本原元**
   （已用独立脚本验证：`ord(2)=51`、`ord(3)=255`）。改为用生成元 **3**
   （g=3，`3v = v XOR 2v`）构造完整 255 项 exp/log 表。修复后 FIPS 197
   已知答案向量（0x57·0x83=0xC1 等）全部通过。
2. **Horner 求值系数顺序错误**：初版把秘密放在了首项系数而非常数项位置，
   导致插值回不到原秘密。改为标准 Horner：从 c_{t-1} 起逐次乘 x 累加，
   最后一步加常数项（秘密）。
3. **API 误用**：`secrets.randrange` 不存在 → 改 `secrets.randbelow`；
   `base64.standard_b64decode(..., validate=)` 不支持该参数 → 统一改
   `base64.b64decode(..., validate=True)`（严格模式只接受标准字母表）。
4. **两处健壮性缺陷**：
   - 超阈值诊断抛 `ConsistencyError` 时未携带解码阶段的 `rejected` 列表，
     已在 `core.recover` 中捕获并附加；
   - 信封标志为 HMAC 但调用方未提供密钥时，初版会按 SHA256 解出"成功"的
     错误语义，已改为 fail-closed 直接 `IntegrityError`。

另有几条**测试预期过度承诺**，核对纠错理论后改为如实断言（代码行为本身是
正确且安全的）：n=t+1（如 2-of-3、3-of-4）且只有 1 个坏份额时，全好组合
仅 1 票并与各错误组合平票，落在 Reed–Solomon 单错误纠正界（需 n ≥ 2t−1）
之外，任何方案都无法在无认证信息时可靠指认坏份额。此类场景服务保证
**检测到不一致并拒绝恢复**，但不声称能定位；定位能力的充分条件
（n ≥ 2t−1、秘密足够长使碰撞可忽略）有对应用例（100/100 定位）。

### 环境插曲

首次运行 `examples/http_demo.sh` 时默认端口 18080 已被本机无关进程
`vccsim`（pid 517275）占用，脚本最初的就绪检查只判断"端口有响应"造成误判。
已改为：默认让内核分配空闲端口，并校验 `/healthz` 响应体确为本服务
（`"service": "tss-local"`），服务进程提前退出时打印日志并失败。复测通过。

## 5. 未通过项 / 已知限制

- 自动化测试、模糊测试、HTTP/CLI 端到端：**无未通过项**。
- 已知能力边界（非缺陷，需求中明确要求说明）：
  1. **无 HMAC 认证的份额无法在密码学意义上识别恶意参与者**：SHA256 标签
     只防意外损坏，持有者可重算；需要识别恶意篡改时必须在 split/recover
     双方带外提供 `auth_key`（HMAC-SHA256），或采用 VSS（本项目未实现）。
  2. 子集投票是**启发式**诊断：份额数仅 n=t+1 等冗余不足时能检出冲突、
     拒绝恢复，但不能可靠指认具体坏份额；冗余充足（经验上 n ≥ 2t−1、
     秘密较长）时才能稳定定位。
  3. GF(2⁸) 逐字节方案上限 **n ≤ 255**；秘密长度上限 1 MiB（本地防 DoS）。
  4. 服务无鉴权、默认仅绑定 127.0.0.1；不要直接暴露到不可信网络。
  5. 纯后端，无前端页面（按需求）。
