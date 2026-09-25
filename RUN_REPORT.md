# 运行记录与验收报告

- 日期：2026-09-24
- 环境：Linux 6.8.0-90-generic (x86_64)，Python **3.12.3**，cryptography **41.0.7**，pytest **9.1.1**
- 依赖安装：`pip install -r requirements.txt`（唯一运行期依赖为 cryptography）
- 结论：**53/53 自动化测试通过**；验收要求的篡改块、交换块（同文件/跨文件）、
  截断、轮换中断、nonce 唯一性、明文临时文件处理均有真实运行证据，**无未通过项**。

## 1. 自动化测试

命令：

```bash
python3 -m pytest tests/ -v
```

结果（末尾摘要，完整逐用例输出见 `examples/pytest-output.txt`）：

```
============================== 53 passed in 3.54s ==============================
```

测试文件与用例数：

| 文件 | 用例数 | 内容 |
|---|---|---|
| tests/test_crypto.py | 12 | 密钥随机性、块/包裹/头 nonce 唯一与域分离、canonical AAD、错误密钥/错误 AAD/篡改密文 |
| tests/test_keystore.py | 4 | 持久化重载、未知密钥、包裹计数器重启不回退、0700/0600 权限 |
| tests/test_service.py | 8 | 0/非对齐大小往返、文件路径、无主密钥、fid 冲突、元数据、包裹密钥缺失、密文无明文特征、崩溃后孤儿 blob 清扫 |
| tests/test_tamper.py | 13 | 篡改密文/tag、同文件换块（整帧与只搬载荷两种）、跨文件换块/换 meta、三类截断、追加帧、魔数、meta/kid 篡改 |
| tests/test_rotation.py | 6 | 只重包 DEK、blob 逐字节不变、指定/多次轮换、同密钥拒绝、nonce 新鲜、中断恢复、多文件独立轮换 |
| tests/test_securetemp.py | 5 | 失败解密无临时明文残留、0600、覆写删除、异常上下文清理、正常提交 |
| tests/test_server.py | 3 | HTTP 全生命周期（真实监听 127.0.0.1 临时端口）、错误码、未知路由 |
| tests/test_cli.py | 2 | 子进程跑真实 CLI 全流程、错误退出码 |

开发过程中曾出现的失败（均已修复并复测通过，如实记录）：

1. 受保护头 nonce 初版只有 9 字节（应为 12 字节）→ 修正为
   `HDR ‖ 8×0x00 ‖ 0x01`；
2. `KeyStore` 构造时无条件重写文件，会覆盖已存在的密钥库 → 改为仅在缺失时
   初始化，存在时校验并收紧权限；
3. crypto.py 缺少 `AEADAuthenticationError` 导入（循环依赖检查后为单向依赖）；
4. "最新主密钥"初版按秒级时间戳排序，同秒内建两把密钥时顺序不确定 → 增加
   持久化单调 `seq` 字段；
5. 两个测试自身的顺序/拼写问题（先建 mk2 再加密导致"同密钥"断言提前触发；
   一处 `svc2` 误写为 `sv2`），均已改正——均为测试代码问题，非实现缺陷。

## 2. 端到端攻击 / 故障演示

命令：

```bash
python3 scripts/demo_attacks.py /tmp/env-demo-attack
```

真实输出（完整见 `examples/demo-attacks-output.txt`，退出码 0）：

```
[1] 加解密往返 OK（25 块，fid=e32519f3fa103643…）
[2] 轮换 OK: mk_5d9e230d3… -> mk_e6848bb39…；blob sha256 不变 32ae2cc98b6cd37d…；轮换后解密一致
  [拒绝] [3] 篡改第 3 块密文 1 字节
         -> AEADAuthenticationError: AEAD 认证失败：密文/nonce/关联数据被篡改，或密钥不正确
  [拒绝] [4] 同文件交换第 0/3 块
         -> AEADAuthenticationError: 第 0 块的 nonce 与块序号派生值不一致（块可能被交换或重放）
  [拒绝] [5] 把另一文件的第 2 块搬入本文件
         -> AEADAuthenticationError: AEAD 认证失败：……
  [拒绝] [6a] 删除尾部 2 个整块（截断）
         -> CorruptContainerError: 实际数据块数 23 与受保护头声明的 25 不一致（文件可能被截断或被追加）
  [拒绝] [6b] 最后一块只写一半
         -> TruncatedContainerError: 容器被截断：期望再读 1724 字节，实际只剩 862 字节
[7a] 轮换中断 OK：旧信封/旧密钥仍可解密，无临时文件残留
[7b] 重启重试 OK：清扫遗留 tmp，轮换到 mk_5f631280b…，明文一致且 blob 未变
总结：全部篡改 / 交换 / 截断均被拒绝；轮换只重包 DEK，中断可安全恢复。
```

## 3. nonce 唯一性实测

命令（要点：30 次加密 + 10 次轮换后枚举所有 `envelope_nonce`，再重启服务继续分配）：

```text
包裹 nonce 总数: 30  唯一数: 30
next_wrap_counter = 41 （已分配 40 次）
重启后新 nonce: V1JBUAAAAAAAAAAp  是否与之前重复: False
块 nonce 派生示例: ['QkxDSwAAAAAAAAAA', 'QkxDSwAAAAAAAAAB', ...]
nonce 唯一性：全部断言通过
```

说明：上面"总数 30"只枚举了当时 30 个文件的当前信封（轮换覆盖前值）；
计数器共分配 40 次（30 加密 + 10 轮换），值 1..40 均为持久化单调分配、无重复；
重启后分配第 41 个值，与历史不重复。块 nonce 由序号确定性派生，
`BLCK ‖ uint64BE(i)` 同文件内天然唯一；跨文件 DEK 不同。

## 4. 明文临时文件实测

- 加密后的 blob/meta 中均搜不到明文特征串 `PLAINTEXT_SENTINEL_abc123_`；
- 正常 `decrypt_file` 后输出与原文一致、目录中无 `.plain*.tmp` 残留，输出文件 0600；
- 篡改 blob 后 `decrypt_file`：抛 `AEADAuthenticationError`，最终文件未生成、
  无临时明文残留（`grep -r` 输出目录仅在合法恢复文件中命中特征串）。

## 5. CLI 与 HTTP 真实记录

- CLI（建钥、加密、轮换前后 sha2556 一致、解密 MATCH、截断报错退出码 2、还原后成功）：
  `examples/cli-session.md`
- HTTP（/health、409、/keys、/encrypt、/decrypt、/rotate、/files、
  篡改后 422、404）：`examples/http-sample.md`

## 6. 未通过项 / 已知限制

- 自动化测试与脚本演示：**无未通过项**。
- 环境插曲：本机 18080/18091 端口被其他进程/代理占用，HTTP 验证改用由内核
  分配空闲端口（`bind(port=0)`）完成；这是环境问题，与代码无关。
- 安全限制（README 第 4.6/7 节亦有说明）：明文临时文件的"覆写删除"在
  CoW/日志型文件系统、SSD FTL、快照、交换分区场景不能保证物理销毁；
  本地 JSON 密钥库仅适合测试，生产应使用 HSM/KMS；HTTP 服务无鉴权、仅回环。
