# 运行报告（RUN_REPORT）

- 日期（UTC）：2026-09-23
- 机器：Linux 6.8.0-90-generic x86_64（Ubuntu 24.04）
- 运行时：Python 3.12.3（/usr/bin/python3）
- 依赖：cryptography 41.0.7（OpenSSL 3.0.13）、pytest 9.1.1（环境中已安装）
- 网络：全程本地 127.0.0.1，无外部账号、无 KMS/HSM、无第三方网络调用

## 1. 执行的命令与结果

| # | 命令 | 结果 |
|---|---|---|
| 1 | `python3 -c "import cryptography"` 环境检查 | ✅ cryptography 41.0.7 可用 |
| 2 | `python3 -m pytest tests/`（最终全套） | ✅ **52 passed in 6.73s** |
| 3 | `python3 -m pytest tests/ -m "not slow"` | ✅ 50 passed, 2 deselected |
| 4 | `bash examples/demo_requests.sh 8091`（真实 HTTP 全流程） | ✅ exit=0，输出见下节 |
| 5 | `python3 -m keyversion --data-dir /tmp/kva-cli init` | ✅ 初始化并产生 v0001 active |
| 6 | `python3 -m keyversion --data-dir /tmp/kva-cli status` | ✅ 输出 1 个版本、1 条审计 |

### 最终测试明细（`pytest --collect-only` 报告 52 tests，全部通过）

```
tests/test_crypto.py...................... 9 个
  AES-GCM 往返、随机 nonce 不确定性、错误密钥拒绝、
  信封版本号 AAD 绑定（换版本号即失败）、单字节篡改拒绝、
  截断/坏魔数拒绝、空与大明文、KEK 包装/错 KEK 拒绝、指纹稳定性
tests/test_state_machine.py............... 9 个
  generate/rotate/activate/deactivate/destroy、
  轮换后唯一 active、幂等激活、全部非法转换拒绝、终态、指针一致性
tests/test_service_permissions.py......... 15 个
  无 active 拒绝加密；retired/generated/destroyed/未知版本加密拒绝；
  retired 历史版本解密；destroyed 不可恢复解密；未知版本/损坏/垃圾信封拒绝；
  交错轮换与加解密（每 3 次加密轮换一次，历史密文全部按版本可解）；
  销毁后重开仍拒绝；base64 辅助
tests/test_persistence_audit.py........... 9 个
  状态重开恢复；销毁后材料确为 null；审计覆盖生命周期且 seq 连续；
  审计落盘字节级无明文/KEK/被包装 DEK；哈希链链接；中间篡改检测；
  末尾半行容忍 / 中间断链拒绝；0600/0700 权限；错 KEK 拒绝启动
tests/test_concurrency.py................. 3 个
  6 线程×10 次轮换后恰好 1 个 active；
  4 加密线程与 1 轮换线程并发，全部密文可按版本解密；
  混合生命周期并发后状态自洽且重开校验通过
tests/test_crash_recovery.py (slow)...... 2 个
  3 轮“子进程高速轮换+加密中 SIGKILL → 重开”，状态不变量/审计链成立，
  已落盘记录全部按版本解密，审计字节无明文；
  SIGKILL 后新子进程干净跑完并最终一致
tests/test_http_api.py.................... 5 个
  health、轮换/错误版本/销毁全流程、生命周期、二进制 base64、
  /audit 无明文、404/400 错误码
                                          ── 合计 52 个
```

### 端到端演示的关键观察（命令 #4，真实服务）

按 `examples/demo_requests.sh` 对 127.0.0.1:8091 实跑，验收点逐条命中：

1. 无 active 时加密 → `409 no_active_version`，审计 seq=1 记 `denied`。
2. `rotate` 产生 `v0001`（active），加密成功（seq=3 success）。
3. 再次 `rotate`：`v0001 → retired`、`v0002 → active`（seq=4/5）。
4. **错误版本拒绝**：显式用 retired 的 v0001 加密 →
   `409 encrypt_version_not_active`（seq=7 denied，含当前 active 指针）。
5. **历史版本解密**：v0001 的旧密文在轮换后仍可解，返回原文与 v0001；
   v0002 新密文同样可解。
6. 篡改密文末字节 → `400 invalid_envelope` / `authentication_failed`（seq=10）。
7. **销毁不可恢复**：`destroy v0001` 后 `has_material=false`，再解密旧密文 →
   `409 decrypt_version_destroyed: key material purged, unrecoverable`（seq=12）。
8. `/audit` 返回 12 条；对 `audit.log` 落盘字节执行
   `grep 'first secret|second secret'` 无任何命中（无明文泄露）。

## 2. 开发过程中出现过、并已修复的问题（如实记录）

均为**开发期测试/实现缺陷**，修复后重跑通过；不代表交付代码的未通过项：

1. **同进程二次打开自死锁**：初版 `open()` 对锁文件直接 `flock(LOCK_EX)`，
   pytest 夹具已持有同目录锁，导致第二次 `open()` 永久阻塞（首跑 120s 超时）。
   修复：改为按数据目录共享的进程内引用计数锁（`_SharedLock`），跨进程仍由
   `flock` 互斥。修复后该套件 0.4s 通过。
2. **一个测试断言写错**：模拟“末尾半行”时用了最后一条完整记录的前半截，
   期望 seq 计算偏差。改为追加一条可区分的伪半行
   `{"seq":99,...`（无换行）后断言正确。
3. **并发生命周期测试的预期外异常**：多线程交错时，某版本可能已被其它线程
   停用，再 `deactivate` 正确返回 `invalid_state_transition`。这是状态机的
   正确拒绝，测试改为把“竞争导致的预期拒绝”与真正损坏区分，只硬校验不变量。
4. **崩溃测试两处脚手架问题**（非产品代码）：
   - 记录文件最初用“整文件复制 + rename 追加”，上万条后 O(n²) 拖慢，
     第二个子进程看似挂起；改为 `O_APPEND` 单次写 + fsync，容忍末尾半行。
   - 父进程初始化后未 `close()` 仍持有 flock，SIGKILL 后的第二个子进程阻塞在
     `locks_lock_inode_wait`（经 `/proc/<pid>/wchan` 定位）；补 `close()`
     释放锁后通过。
5. 一次审计篡改测试选取的扰动字节位置可能恰好无变化，改为解析 JSON、
   改写 `detail` 后重写，稳定触发哈希链失败。

## 3. 未通过项 / 已知限制

- **最终交付状态：全部 52 个自动化用例通过，CLI 与 HTTP 端到端实跑通过，
  无未通过项。**
- 设计上明确不做的事（非缺陷）：
  - 无多用户鉴权/RBAC（本地单租户，仅绑 127.0.0.1）；
  - KEK 与数据同机存放（`data/kek.key`），能防止状态文件被单独拷贝/替换，
    不能防御整机失陷；生产方案应换为 HSM/KMS，本任务明确不接生产账号；
  - 无密钥材料的内存清零保证（Python bytes 不可变，无法可靠擦除）；
  - 审计“状态已提交、审计最后一条丢失”的崩溃窗口按设计容忍（见 README §6），
    状态机始终自洽；中间任何条目的篡改/删除仍会被哈希链检出。
