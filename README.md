# 阈值份额恢复（Threshold Secret-Sharing，TSS）

纯后端、本地运行的安全数据处理服务：用 **Shamir 秘密分享（Shamir's Secret Sharing）**
把秘密拆成 `n` 个份额，持有任意至少 `t`（阈值）个份额即可恢复；少于 `t` 个份额
在信息论意义上得不到秘密的任何信息。

- 语言/运行时：Python 3（标准库 HTTP 服务），密码原语使用成熟库
  [`cryptography`](https://cryptography.io/)（HMAC）与标准库 `secrets`/`hashlib`；
- **不自创加密算法、不自创有限域参数**：有限域为 AES 的标准域 GF(2⁸)
  （Rijndael 不可约多项式 x⁸+x⁴+x³+x+1，0x11B）；
- 份额为**带版本号的自描述二进制信封**，base64 文本承载；
- 检测重复横坐标、拒绝跨批次（混批）份额、隔离处理损坏编码；
- 测试密钥全部本地 CSPRNG 生成；**不接任何生产账号、无外部网络依赖、无前端**。

## 目录结构

```
tss/
  gf.py         GF(2^8) Rijndael 域（查表，含 FIPS 197 已知答案向量）
  shamir.py     逐字节 Shamir：split / Lagrange 恢复 / 子集投票诊断
  envelope.py   TSS1 带版本份额信封：魔数+版本+域+参数+split_id+载荷+完整性标签
  core.py       高层流程（拆分/恢复/校验），HTTP 与 CLI 共用
  service.py    本地 HTTP 服务（默认 127.0.0.1:8080，纯标准库）
  cli.py        命令行入口
  errors.py     带稳定错误码的异常
tests/          自动化测试（pytest / unittest）
examples/       请求样例与端到端演示脚本
```

## 快速开始

```bash
pip install -r requirements.txt        # 仅依赖 cryptography
python -m pytest -q                    # 运行全部测试
bash examples/http_demo.sh             # 端到端 HTTP 演示（自动起停服务）
```

启动本地服务：

```bash
python -m tss.cli serve --host 127.0.0.1 --port 8080
```

## HTTP 接口

所有接口均为 JSON。默认只绑定回环地址；无鉴权（本地服务，威胁模型见文末）。

### `GET /healthz`
健康检查。

### `POST /split` — 拆分
请求（秘密二选一：`secret_text` 为 UTF-8 文本，或 `secret_b64` 为标准 base64）：

```json
{ "secret_text": "需要保护的秘密", "threshold": 3, "total": 5 }
```

带 HMAC 认证时额外给 `auth_key_b64`（≥16 字节的 base64 密钥，建议 32 字节；
密钥必须带外分发）。响应：

```json
{
  "split_id": "9f1c…(32 hex)",
  "threshold": 3, "total": 5, "authenticated": false,
  "shares": ["VEFTMQ…(base64 信封)", "…"]
}
```

### `POST /recover` — 恢复
```json
{ "shares": ["…", "…", "…"], "auth_key_b64": "可选" }
```
给 ≥ `t` 个**不同横坐标**的份额即可。可以多给：超过阈值的份额会被用于子集
一致性诊断。响应返回 `secret_b64`、`secret_text`（UTF-8 失败时替换字符）、
实际使用的份额数与被隔离拒绝的 `rejected` 列表。

### `POST /validate` — 校验单个信封
```json
{ "share": "…", "auth_key_b64": "可选" }
```
返回该份额声明的版本、有限域、`(t,n)`、横坐标、`split_id`、载荷长度、是否认证。

### 错误响应与状态码

| HTTP | error 码 | 含义 |
|---|---|---|
| 400 | `invalid_params` / `malformed_share` / `integrity_failure` | 参数错 / 信封编码或版本不合法 / 完整性标签失配 |
| 409 | `below_threshold` | 有效份额少于阈值，拒绝恢复 |
| 409 | `duplicate_index` | 请求中出现重复横坐标 |
| 409 | `inconsistent_shares` | 混批（split_id 不一致）、参数不一致、子集恢复结果冲突 |

错误体还可能带 `rejected`（逐份额拒绝原因）、`suspect`（启发式可疑下标，
0-based 请求位置）、`votes`（子集投票统计）。

## 份额信封格式（版本 1，`TSS1`）

大端二进制，整体做标准 base64：

| 偏移 | 长度 | 字段 |
|---|---|---|
| 0 | 4 | 魔数 `TSS1` |
| 4 | 1 | 格式版本（=1；未知版本 fail-closed 拒绝） |
| 5 | 1 | 有限域标签（`0x01` = GF(2⁸)/Rijndael 0x11B） |
| 6 | 1 | 标志位：bit0=1 为 HMAC-SHA256 标签，否则为纯 SHA256 校验和 |
| 7 | 1 | 阈值 t |
| 8 | 1 | 份额总数 n（2 ≤ t ≤ n ≤ 255） |
| 9 | 1 | 本份额横坐标 x（1..255，拆分时固定取 1..n） |
| 10 | 16 | `split_id`：同一次拆分的随机实例标识，用于拒绝混批 |
| 26 | 4 | 载荷长度 L |
| 30 | L | 载荷（该点纵坐标，长度等于秘密长度；秘密按字节逐元素分享） |
| 30+L | 32 | 完整性标签（覆盖前 30+L 字节，恒定时间比较） |

## 命令行

```bash
# 拆分（文本/文件/base64）
python -m tss.cli split --text "hello" -t 3 -n 5 --json
python -m tss.cli split --secret-file key.bin -t 2 -n 3 > shares.txt

# 恢复（每行一个份额，# 开头为注释）
grep -v '^#' shares.txt | sed -n '1p;3p;5p' | python3 -m tss.cli recover

# 生成 HMAC 认证密钥 / 校验单个份额
KEY=$(python -m tss.cli gen-auth-key)
python -m tss.cli split --text "hi" -t 2 -n 3 --auth-key-b64 "$KEY" --json
head -1 shares.txt | python -m tss.cli validate --auth-key-b64 "$KEY"
```

## 密码学设计与安全边界（务必阅读）

1. **Shamir over GF(2⁸)，逐字节分享。** 随机多项式
   `P(x)=s+c₁x+…+cₜ₋₁xᵗ⁻¹`，秘密为常数项；份额为 `(i, P(i))`；
   在 x=0 处拉格朗日插值恢复。系数由操作系统 CSPRNG（`secrets`）产生。
   少于 t 个份额对秘密不提供任何信息（信息论安全，per byte）。
   n 上限 255（非零横坐标数量）。
2. **无认证份额无法（在密码学意义上）识别恶意参与者。** 这是本方案刻意明确的
   边界，而不是缺陷：
   - 不带 `auth_key` 时，份额只附 **SHA256 校验和**。它能发现**意外损坏**
     （传输/编码/存储错误），但任何持有份额的人都能在篡改后重算校验和，
     所以它**不能**识别主动伪造；
   - 带 `auth_key` 时，标签为 **HMAC-SHA256**（`cryptography` 实现）。
     攻击者不知道带外分发的密钥，无法伪造合法标签，恶意份额在标签层即被拒绝。
     这是本服务识别恶意份额的推荐方式（更强的方案是可验证秘密分享 VSS，
     本项目未实现）；
   - 对成功解码但相互矛盾的份额，服务会在份额数 **超过阈值** 时做
     **子集投票**（枚举/抽样所有 t 元子集并比较恢复结果）作为最佳努力诊断，
     能在冗余充足（经验上 n ≥ 2t−1、秘密足够长）时把坏份额列入 `suspect`。
     这只是启发式：当 t 个恶意份额同时进入、或 n=t+1 平票（Reed–Solomon
     纠错界之外）时无法可靠指认，服务一律**拒绝恢复**，绝不静默输出错误秘密。
3. **混批与重复横坐标。** `split_id` 不匹配、`(t,n)` 不匹配、认证标志混用、
   同一横坐标重复出现，全部拒绝恢复。
4. **机密性不在网络/存储层额外加密。** 服务默认只监听 127.0.0.1；
   份额文本与秘密以 JSON 在本地回环上传输。不要把服务直接暴露到不可信网络；
   需要远程使用时请自行套本机可信通道并做好进程与文件权限管理。
5. 随机数、侧信道：随机性来自操作系统；标签比较使用恒定时间
   `hmac.compare_digest`。其余字段比较不涉及秘密比较。

## 验收项对照

| 验收要求 | 实现位置 / 测试 |
|---|---|
| 任意阈值子集恢复 | `shamir.combine_points`；`tests/test_shamir.py`（枚举全部 t 元子集）、`tests/test_core.py::test_recover_any_threshold_subset`、200 轮随机模糊测试 |
| 少于阈值拒绝接口恢复 | `shamir.combine_points` / `core.recover` 抛 `below_threshold`；HTTP 409；测试 `test_below_threshold_*` |
| 混批份额处理 | 16 字节 `split_id` 比对；`test_mixed_batch*` |
| 损坏编码处理 | 逐份额独立解码、隔离进 `rejected`；`test_corrupted_base64_*`、`test_*checksum*` |
| 重复横坐标检测 | `test_duplicate_x_*` |
| 参数/份额格式带版本 | 信封魔数+版本+域标签，未知版本拒绝；`tests/test_envelope.py` |
| 成熟有限域 | GF(2⁸)/0x11B，含 FIPS 197 已知答案向量与域运算性质测试 |
| 无认证份额不能识别恶意者 | 明确区分 SHA256（防意外）与 HMAC（防伪造）；含"重算校验和攻击成功"的测试与文档说明 |

## 运行记录

实际命令、输出与未通过项记录在 [`RUNLOG.md`](./RUNLOG.md)。
