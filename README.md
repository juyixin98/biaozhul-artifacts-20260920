# 信封加密与主密钥轮换（本地安全数据处理服务）

纯后端 Python 项目：对文件做**分块 AEAD（AES-256-GCM）**加密，数据密钥（DEK）由
主密钥（MK）以信封方式包裹；**轮换主密钥时只重新包裹 DEK，不重加密任何数据块**。

约束与定位：

- 密码原语只用成熟库 [`cryptography`](https://cryptography.io/)（AES-256-GCM），
  **不自创任何密码算法**；
- 主密钥 / 数据密钥全部本地用 `os.urandom` 生成，**不接任何生产账号 / KMS**；
- 本地工具 / `127.0.0.1` HTTP 服务，不做前端；
- 目录与文件默认 `0700/0600` 权限。

---

## 1. 目录结构

```
envelope/
  crypto.py       原语封装：AES-256-GCM、nonce 派生、AAD 构造
  blob.py         分块密文容器（BLOB1）：帧编码、逐块认证解密
  meta.py         信封元数据（ENVLP）：DEK 信封 + DEK 加密的受保护头
  keystore.py     本地主密钥库（keystore.json，0600）+ 包裹 nonce 持久计数器
  securetemp.py   明文临时文件：0600、原子 rename、失败覆写删除
  service.py      核心服务：加解密、轮换、故障注入钩子、崩溃恢复清扫
  server.py       仅绑定 127.0.0.1 的 HTTP 接口（标准库 http.server）
  cli.py          命令行（python -m envelope ...）
tests/            53 个 pytest 用例
scripts/
  demo_attacks.py 端到端攻击 / 故障演示脚本（无需 pytest）
examples/         真实运行输出（pytest、攻击演示、HTTP 请求/响应样例）
```

## 2. 安装与快速开始

需要 Python 3.10+（开发与验证环境为 Python 3.12.3）。

```bash
pip install -r requirements.txt        # cryptography>=41
python -m pytest tests/ -q             # 53 passed

# 命令行全流程（存储目录默认 ./envelope-store）
python -m envelope --store ./store new-key
python -m envelope --store ./store encrypt secret.txt --chunk-size 65536
python -m envelope --store ./store info <file_id>
python -m envelope --store ./store decrypt <file_id> recovered.txt
cmp secret.txt recovered.txt
python -m envelope --store ./store rotate <file_id>     # 新主密钥重包 DEK
python -m envelope --store ./store list
python -m envelope --store ./store list-keys

# HTTP 服务（只监听回环地址，默认端口 8080）
python -m envelope --store ./store serve --port 8080
```

更多 HTTP 请求 / 响应见 [`examples/http-sample.md`](examples/http-sample.md)；
CLI 样例见 [`examples/cli-session.md`](examples/cli-session.md)。

## 3. 数据模型与磁盘格式

```
明文文件
  │  每文件随机生成 DEK（AES-256）
  ▼  分块 AES-256-GCM；nonce=块序号派生；AAD=文件ID+块序号+分块参数
blobs/<fid>.blob
  │  DEK 用主密钥 MK 包裹（AES-256-GCM，持久计数器 nonce，AAD=文件ID）
  ▼  与"DEK 加密的受保护头"一起存为
meta/<fid>.meta
```

- **fid（文件 ID）**：128 位随机十六进制；既作为文件名，也进入所有 AEAD 的 AAD。
- **blob 容器**：魔数 `BLOB1` 后接若干帧，每帧
  `uint32BE 帧长度 || nonce(12) || 密文 || GCM tag(16)`。
- **meta 文件**：魔数 `ENVLP` + 长度前缀的路由 JSON（fid、kid、DEK 信封、
  受保护头密文）。
- **受保护头**（由 DEK 的 GCM tag 认证）：算法、fid、明文总长、分块大小、块数、
  `header_version`。密文侧的一切规模声明都在此处，攻击者改不动。

## 4. 关键安全设计

### 4.1 nonce 唯一性（GCM 的硬性要求：同一密钥下 nonce 绝不可重复）

| nonce | 作用域 | 生成方式 | 为什么唯一 |
|---|---|---|---|
| 数据块 nonce | 单文件 + 单 DEK | `BLCK ‖ uint64BE(块序号)` 确定性派生 | 同文件序号天然唯一；不同文件 DEK 是不同随机密钥，密钥域独立 |
| DEK 包裹 nonce | 单主密钥 | `WRAP ‖ uint64BE(计数器)` | 计数器在密钥库中持久化、单调递增，**分配前先 fsync 落盘** |
| 受保护头 nonce | 单 DEK（每信封只封装 1 次头） | 固定 `HDR ‖ 0…01` | 每文件随机 DEK，密钥域唯一；轮换时 DEK 不变但只重包/重封一次 |

包裹计数器在密钥库 JSON 的 `next_wrap_counter` 中持久化，通过
`临时文件 + fsync + 原子 rename + 目录 fsync` 落盘；崩溃可能造成计数器空洞
（跳过若干 nonce），但**绝不会回退或复用**。另有 `flock` 串行化同机多进程。
实测 30 次加密 + 10 次轮换共 40 个包裹 nonce 全唯一，重启后继续递增不复用
（见运行报告）。

### 4.2 关联数据（AAD）绑定

- 数据块 AAD 绑定 `文件ID + 块序号 + 分块大小 + 算法 + 版本`：
  - 同文件调换块 → 帧 nonce 与序号不符（先被显式校验拒绝），即使只搬密文保留
    nonce，AAD 中的序号变化也使 GCM tag 失败；
  - 跨文件搬块 → 文件 ID 不同且 DEK 不同，认证失败；
  - 改分块参数 → AAD 不匹配。
- DEK 信封 AAD 与受保护头 AAD 都绑定文件 ID：整套 meta+blob 改名搬运同样失败。
- AAD 用键排序、无空白的 canonical JSON，保证两端字节一致。

### 4.3 截断 / 追加检测

逐块解密时：帧头不完整 → `TruncatedContainerError`；帧载荷不完整 → 截断错误；
干净读到 EOF 后，实际块数必须等于受保护头声明的块数，否则
`CorruptContainerError`；明文总长度还要与受保护头的 `plaintext_size` 一致。
追加伪造帧会因 nonce/序号或块数不符被拒。帧长度字段有硬上限（≤16 MiB 块），
伪造超大长度不会造成无界内存分配。

### 4.4 轮换主密钥：只重包 DEK

`rotate_master_key` 用旧 MK 解开 DEK（DEK 只在内存中短暂出现，不落盘），
用新 MK + 新包裹计数器重新包裹 DEK，并用 DEK 重新封装受保护头
（`header_version += 1`），然后**原子替换 meta**；blob 不打开、不重写，
轮换后自检 blob 与轮换前逐字节相同，返回 `blob_bytes_changed=0`。
同一密钥"轮换"会被拒绝（`RotationError`）。

### 4.5 轮换中断与崩溃恢复

meta 替换采用 `写 0600 临时文件 → fsync → rename → fsync 目录`，崩溃只可能
发生在替换前（旧文件完好）或替换后（新文件已落盘），不存在半个 meta。
- 替换前崩溃：旧信封仍可用旧 MK 正常解密，直接重试即可；
- 服务启动时自动清扫 `meta/`、`blobs/` 下的 `.*.tmp` 残留；若 blob 已落盘但
  meta 未提交（两步之间被杀），无 meta 的**孤儿 blob** 也会在启动时删除
  （没有 meta 就没有 DEK，该 blob 在密码学上已不可解读）。

测试通过故障注入钩子模拟"新 meta 已就绪、rename 前被杀"，并模拟遗留临时文件
后"重启"，验证旧文件可用、重试成功、明文与 blob 均不变
（`tests/test_rotation.py::test_interrupted_rotation_is_recoverable`）。

### 4.6 明文临时文件

- 临时明文以 `mkstemp`（`O_EXCL` 唯一名）+ `fchmod 0600` 创建在目标同目录；
- 全部 AEAD 校验通过后才原子 rename 为最终文件；任何异常路径都先
  随机字节覆写、fsync，再删除；
- 优先使用流式接口（`encrypt_stream/decrypt_stream`），调用方可完全不落明文。

**如实说明的局限**：覆写删除在写时复制 / 日志文件系统（btrfs、ZFS、APFS）、
SSD 耗损均衡、快照、交换分区等场景下不能保证旧字节被物理销毁。强保护应配合
tmpfs 或全盘加密（LUKS）；源明文文件是否删除由调用方按威胁模型决定
（可调用 `securetemp.secure_unlink`，但同样受上述限制）。

## 5. HTTP 接口（仅 127.0.0.1）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/keys` | 生成新主密钥 |
| GET  | `/keys` | 列出主密钥 |
| POST | `/encrypt` | body `{"data_b64": ..., "chunk_size": 可选}` |
| GET  | `/decrypt?file_id=...` | 返回 `{"data_b64": ...}` |
| GET  | `/files`、`/files/<fid>` | 列表 / 信封信息 |
| POST | `/rotate` | body `{"file_id": ..., "new_kid": 可选}` |

状态码：400 参数错误；404 文件/密钥不存在；409 无主密钥 / 目标密钥相同 /
文件已存在；422 密文侧 AEAD 或完整性校验失败。明文经 base64 在本地回环承载，
**不要把该服务暴露到网络**，也没有任何鉴权。

## 6. 自动化测试与验收映射

`python -m pytest tests/ -v` —— **53 passed**（完整输出见
[`examples/pytest-output.txt`](examples/pytest-output.txt)）。

| 验收项 | 覆盖用例 |
|---|---|
| 篡改数据块（密文/tag 各一） | `test_tamper_ciphertext_byte_in_block`、`test_tamper_tag_byte_in_block` |
| 同文件交换块（整帧 / 只搬密文） | `test_swap_blocks_within_same_file`、`test_swap_payload_keeping_nonce` |
| 跨文件交换块 / 交换 meta | `test_cross_file_block_swap`、`test_cross_file_meta_swap` |
| 截断（删整帧 / 切半帧 / 帧头残） | `test_truncate_drop_whole_frames`、`test_truncate_last_frame_body`、`test_truncate_inside_frame_header` |
| 追加伪造帧 / 魔数篡改 / meta 篡改 | `test_append_garbage_frame`、`test_tamper_magic`、`test_tamper_meta_envelope_bytes`、`test_tamper_meta_kid` |
| 轮换只重包 DEK、blob 不变、多次轮换 | `test/test_rotation.py` 全部 |
| 轮换中断可恢复 | `test_interrupted_rotation_is_recoverable` |
| nonce 唯一性（域分离/计数器/重启） | `test_crypto.py`、`test_wrap_counter_persists_and_never_repeats`、`test_rotation_uses_fresh_wrap_nonces` |
| 明文临时文件（失败即清、权限） | `tests/test_securetemp.py` |
| HTTP / CLI 端到端 | `tests/test_server.py`、`tests/test_cli.py` |

端到端攻击脚本（真实拒绝记录见
[`examples/demo-attacks-output.txt`](examples/demo-attacks-output.txt)）：

```bash
python3 scripts/demo_attacks.py /tmp/demo-store
```

## 7. 安全边界与非目标

- 这是本地教学 / 测试级实现，不是生产 KMS：主密钥以 base64 存于本地 JSON。
  生产应使用 HSM/KMS、最小权限与审计。
- 不提供网络鉴权 / TLS / 多租户隔离；HTTP 仅回环。
- 单文件块数受 uint64 与 GCM 每密钥 2^32 nonce 上限约束，实际分块下不可达；
  超大总量应结合主密钥轮换策略管理。
- 不做重放保护之外的并发多写协调（同一 fid 重复写入会被显式拒绝覆盖）。
