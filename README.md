# 阈值份额恢复 (Threshold Share Recovery)

纯后端、本地运行的 **Shamir 秘密分享 (Shamir's Secret Sharing, SSS)** 数据处理服务。
把一个秘密切成 `n` 个份额，凑齐任意 `k` 个（阈值）即可恢复；少于 `k` 个在接口层被拒绝。

- **不做前端**，只提供本地 HTTP API、Python 库 API 和命令行。
- **不接生产账号**；所有测试密钥/份额在本地用操作系统 CSPRNG 临时生成。
- **不自创加密算法**：密码学原语全部来自成熟库 [`cryptography`](https://cryptography.io/)
  （仅用于可选的口令封装 scrypt + AES-GCM），Shamir 数学构建在**标准公开有限域**
  secp256k1 素数域 GF(p) 上（只用素数域本身，不涉及椭圆曲线运算）。

> 版本：参数与份额格式带版本号（scheme/format `SSS/1`，紧凑份额前缀 `SSS1$`）。
> 读取到未知版本/未知扩展位时直接报错，不猜测、不静默忽略。

---

## 1. 安全模型与重要限制（请先读）

### 能做什么

- 任意 `k` 个**不同横坐标**的份额恢复同一秘密；`k-1` 个份额在信息论意义上不泄露秘密。
- 服务端**故障闭合 (fail-closed)**：份额无法全部解码、参数批次不一致、横坐标重复、
  或数量不足时，一律拒绝恢复，绝不返回“部分结果”。
- 可选的**指纹校验**：切分时返回秘密的 `sha256` 指纹；恢复时回传指纹即可检测
  份额是否被篡改 / 参与者是否提交了错误份额。

### 不能做什么（关键）

**普通 Shamir 份额是无认证的 (unauthenticated)：服务无法自动识别恶意参与者。**

如果有人提交了伪造或篡改过的份额：

- 恢复结果会是错误的字节，或在长度/填充结构检查处失败；
- 指纹校验能发现“**有**份额不对”（接口返回 `recovered_secret_invalid` / HTTP 422），
  但错误信息**无法指出具体是哪一份 / 哪一个参与者**——这是无认证 SSS 的固有性质，
  不是实现缺陷。要指认恶意者需要额外的**可验证秘密分享 (VSS / Feldman、Pedersen)**
  或份额签名/承诺体系，本项目未实现，也没有用任何自创机制伪装成具备该能力。

`seal_share` / `/v1/seal` 提供的 AES-GCM + scrypt 口令封装只是一个**独立的认证示例**
（份额持有者自己的口令与密文之间的认证，GCM 会检测密文/头部篡改），它不改变
“组合方无法认证份额、无法指认参与者”这一结论。

### 其他边界

- 默认只监听 `127.0.0.1`；请求体上限 1 MiB；服务本身不带任何身份认证，绑定到
  非回环地址需操作者自行评估网络暴露风险。
- 份额字符串本身等同于秘密的等效材料，输出到终端/文件/ shell 历史记录时需自行保护。

---

## 2. 方案与份额格式

### 2.1 数学方案 SSS/1

| 项目 | 取值 |
|---|---|
| 有限域 | GF(p)，`p` = secp256k1 域素数 = 2²⁵⁶ − 2³² − 977（标准公开模数） |
| 块大小 | 31 字节 (248 bit)，每个块都严格小于 p；秘密按块切分，每块独立生成随机多项式 |
| 秘密封装 | 4 字节大端长度前缀 + 秘密，零填充到块边界；恢复后按长度前缀裁掉填充 |
| 多项式 | 每块独立的随机 `degree = k-1` 多项式，常数项 = 块整数，系数取自已校验 CSPRNG (`secrets`) 的 248-bit 块空间 |
| 份额 | 横坐标 `x ∈ 1..n`（`n ≤ 255`），每块一个域元素 y |
| 恢复 | 对每个块做 GF(p) 上的拉格朗日插值求 f(0)（逆元用标准模幂 `a^(p-2)`） |

> 实现说明：系数限定在 248 位块空间，使线性 Shamir 在该空间上封闭，
> 保证插值常数项能无损编码回 31 字节；x ≤ 255，求值远小于 p，归约无误。
> 恢复阶段若发现插值元素跑出块空间（篡改特征），转为结构化错误而不是崩溃。

### 2.2 份额线格式（版本化）

紧凑二进制，再 base64url（**无填充**），前缀 `SSS1$`：

```
"SSS1" | flags(1) | x(1) | threshold(2 BE) | total(2 BE) | y_0(32 BE) | y_1 ... 
```

- 每份份额自带 `threshold`/`total`/版本，恢复时**逐份校验参数一致**，防止混批。
- 解码严格：非规范 base64（空白、`+`、`/`、`=`、换行）、截断、`y ≥ p`、
  `x` 越界、未知 flags、未知 magic（如 `SSS2`）全部报错。
- 另有等价的 JSON 对象形式（`version/x/threshold/total/y[]`），未知字段直接报
  “版本不支持”，避免前向兼容时静默丢弃安全相关扩展。
- 秘密**长度不存放在份额头里**（它在被分享的数据内部），无法从单份份额伪造长度头。

---

## 3. 快速开始

```bash
pip install -r requirements.txt        # cryptography, pytest（环境里已装可跳过）
python3 -m pytest tests/ -q            # 运行自动化测试
bash examples/demo.sh                  # 端到端演示（自动起停本地服务）
```

起服务：

```bash
python3 -m threshold_shares.cli serve --port 8080
# threshold-shares 1.0.0 listening on http://127.0.0.1:8080
```

健康检查：

```bash
curl -s http://127.0.0.1:8080/healthz
# {"status":"ok","version":"1.0.0","scheme_version":1}
```

### 切分（3-of-5）

```bash
curl -s -X POST http://127.0.0.1:8080/v1/split \
  -H 'Content-Type: application/json' \
  -d '{"threshold":3,"total":5,"secret_text":"correct horse battery staple"}'
```

请求体（`secret_text` 与 `secret_b64` 二选一，后者为标准 base64）：

```json
{ "threshold": 3, "total": 5, "secret_b64": "aGVsbG8=" }
```

响应节选：

```json
{
  "version": 1,
  "field": "secp256k1-prime",
  "threshold": 3,
  "total": 5,
  "shares": ["SSS1$...", "SSS1$...", "... 共 5 条 ..."],
  "shares_json": [ { "version": 1, "x": 1, "...": "..." } ],
  "secret_fingerprint": "sha256:c4bbcb1f..."
}
```

### 恢复（任意 3 份，带指纹校验）

```bash
curl -s -X POST http://127.0.0.1:8080/v1/recover \
  -H 'Content-Type: application/json' \
  -d @examples/recover_request.json
```

```json
{
  "version": 1, "field": "secp256k1-prime",
  "threshold": 3, "total": 5,
  "used_positions": [0, 1, 2],
  "used_indices": [1, 3, 5],
  "unused_positions": [],
  "valid_share_count": 3,
  "secret_b64": "Y29ycmVjdCBob3JzZSBiYXR0ZXJ5IHN0YXBsZQ==",
  "secret_length": 28,
  "secret_fingerprint": "sha256:c4bbcb1f...",
  "fingerprint_matches": true
}
```

`shares` 中可混用 `SSS1$` 字符串与 JSON 对象形式。多给的有效份额不会参与插值，
其位置出现在 `unused_positions`。

### 错误码（均为 JSON：`{"error":{code,message,details}}`，不含秘密材料）

| 场景 | code | HTTP |
|---|---|---|
| 少于阈值 | `insufficient_shares` | 400，details 含 `required/received/missing/total` |
| 重复横坐标 | `duplicate_share_index` | 400，details 给出 `x` 与位置 |
| 参数批次不同（混批） | `mixed_batch` | 400，列出期望参数与每份不匹配项 |
| 损坏/非规范编码 | `invalid_share_encoding` | 400，逐份给出位置和原因 |
| 指纹不符 / 结构损坏 | `recovered_secret_invalid` | 422，明确说明无法指认恶意参与者 |
| 参数非法 / JSON 非法 / 路径不存在 / body 过大 | `invalid_parameters` `bad_json` `not_found` `body_too_large` | 400 / 400 / 404 / 413 |

### 可选：口令封装份额（认证示例）

```bash
# 封装
curl -s -X POST http://127.0.0.1:8080/v1/seal -H 'Content-Type: application/json' \
  -d '{"share":"SSS1$...","passphrase":"pw","scrypt_n":4096}'
# {"sealed_share":"SEAL1$..."}
# 解封装（口令错误或 token 被改 -> 400 sealing_error）
curl -s -X POST http://127.0.0.1:8080/v1/unseal -H 'Content-Type: application/json' \
  -d '{"sealed_share":"SEAL1$...","passphrase":"pw"}'
```

令牌格式 `SEAL1$` + base64url(`SEAL1` | salt16 | scrypt n/r/p | nonce12 | AES-256-GCM 密文+tag)，
头部参数作为 GCM 的 AAD 一并认证。

### 命令行

```bash
python3 -m threshold_shares.cli split -k 3 -n 5 --text "hello" --out shares.json
python3 -m threshold_shares.cli recover shares.json --expect-fingerprint sha256:... --secret-out out.bin
python3 -m threshold_shares.cli recover /tmp/empty.json --share "SSS1\$..." --share "SSS1\$..."
python3 -m threshold_shares.cli seal   "SSS1\$..." --passphrase-file pw.txt
python3 -m threshold_shares.cli unseal "SEAL1\$..." --passphrase-file pw.txt
```

### 库 API

```python
from threshold_shares import service

bundle   = service.split(b"my secret", threshold=3, total=5)
recover  = service.recover_from_encoded(
    [bundle["shares"][0], bundle["shares"][2], bundle["shares"][4]],
    expected_fingerprint=bundle["secret_fingerprint"],
)
assert recover["fingerprint_matches"] is True
```

---

## 4. 验收场景

均在 `examples/demo.sh`（真实 HTTP）与 `tests/`（自动化）中覆盖：

1. **任意阈值子集恢复** — 3-of-5 的全部 C(5,3)=10 个组合逐一验证；
   另有 (k,n) ∈ {2-2…10-10} 与 15 组随机参数化属性测试。
2. **少于阈值拒绝** — 接口层直接返回 `insufficient_shares`，响应不含秘密字段；
   核心层额外验证 k-1 个点在数学上无法还原常数项。
3. **混批份额** — 不同 threshold/total、或不同块数（不同秘密长度）的份额混入，
   返回 `mixed_batch` 并标注位置。
4. **损坏编码处理** — 非规范 base64、截断、`y≥p`、`x` 越界、未知版本/字段、
   JSON 类型错误，全部 `invalid_share_encoding`；篡改 y 值 + 指纹校验返回 422，
   且错误信息明确“不能识别具体恶意参与者”。

---

## 5. 项目结构

```
threshold_shares/
  field.py       secp256k1 素数域运算：加/减/乘/逆元、多项式求值、x=0 拉格朗日插值
  scheme.py      Shamir SSS/1：参数校验、分块/长度前缀、split_secret / recover_secret、Share
  encoding.py    版本化份额编码：SSS1$ 紧凑串 + JSON 对象；严格解码/版本检测
  sealing.py     可选口令封装：scrypt + AES-256-GCM（SEAL1$），认证示例
  service.py     应用层：split / recover_from_encoded，故障闭合校验与错误类型
  server.py      仅本地的 stdlib HTTP 服务（/healthz /v1/split /v1/recover /v1/seal /v1/unseal）
  cli.py         命令行：serve / split / recover / seal / unseal
tests/           72 个自动化测试（域数学、方案、编码、服务、活的 HTTP 端到端）
examples/        请求样例、demo.sh、真实运行输出
RUNLOG.md        实际运行命令与结果记录（含曾经失败的项）
```

## 6. 开发约束（自查）

- 加密原语只来自 `cryptography`；随机数只来自 `secrets`/`os.urandom`。
- 无自加密算法、无生产账号、无外部网络依赖；服务默认绑定回环地址。
- 错误对象/HTTP 响应不携带秘密字节；篡改检测只说“有问题”，不冒充可指认攻击者。
