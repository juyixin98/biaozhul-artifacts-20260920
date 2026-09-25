# 运行记录（RUNLOG）

本机环境：Ubuntu（Linux 6.8.0-90-generic），Python 3.12.3，
cryptography 41.0.7。以下命令与输出均为本项目实际运行所得，
如实记录，包括开发过程中发现的 bug、一次我对 RFC 验证语义的错误
断言及其澄清过程。

## 1. 环境检查

```text
$ python3 --version
Python 3.12.3
$ python3 -c "import cryptography; print(cryptography.__version__)"
41.0.7
```

## 2. 自动化测试

```text
$ python3 -m unittest discover -s tests
...
Ran 57 tests in 2.447s

OK
```

57 个用例，0 失败、0 错误。覆盖：

- 硬编码哈希锚点（空树根、单空叶根）；
- n=1..33 小树全部 561 片叶子的逐项包含验证；
- 非二次幂大小 3/5/6/7/9/15/17/31/33/63/65/127/129/255/257/511/513/1000/1023/1025；
- 三套独立实现交叉比对（RFC 翻译版 / 自底向上参考版 / 手工锚点）；
- RFC 6962 七叶树符号向量逐元素核验；
- 一致性证明 n=2..39 全组合 + 更大树关键位置；
- 伪造旧根、错误索引、路径篡改/截断/追加/乱序、畸形输入；
- Ed25519 签名五类篡改拒绝；
- leaves.jsonl 改内容/插入/删除被哈希链发现；
- HTTP API 端到端（含 400/404）。

## 3. 端到端演示

```text
$ python3 scripts/demo.py
```

关键输出（完整输出在开发时逐行核对，摘要如下）：

```text
1. 追加 13 条叶子（非二次幂）
tree_size = 13
SHA256 树根 = 230db6f29833dfd08e3fdf07f4752b137ccf53964cbf133cb0f21e15d50fab9a

2. STH 验签： 通过 ✓
3. 对全部 13 片叶子逐项生成并验证包含证明：13/13 全部通过：True
   （叶子 0..11 路径长度 4；叶子 12 路径长度 2 —— 非二次幂树的典型不对称）

4. 攻击演示（验证器必须全部拒绝）
4a. 伪造旧根（首字节翻转）验证包含证明： 拒绝 ✓
4b. 用错误索引 0/2/4/12 验证同一份证明： 全部拒绝 ✓
4c. 篡改路径首元素： 拒绝 ✓
4d. 路径截断（少一个兄弟）： 拒绝 ✓
4e. STH 树大小被改成 14 后验签： 拒绝 ✓

5. 追加到 20 叶
  一致性 PROOF(first= 1, n=20): 路径长度=5  验证=✓
  一致性 PROOF(first= 4, n=20): 路径长度=3  验证=✓
  一致性 PROOF(first= 7, n=20): 路径长度=6  验证=✓
  一致性 PROOF(first=13, n=20): 路径长度=6  验证=✓
→ 旧 STH(13 叶) 与新 STH(20 叶) 前缀一致性：通过 ✓

6. 在 size=13 的旧根上验证叶子 0 的历史包含证明：通过 ✓
7. 明确声明不解决日志分叉/共谋。
```

## 4. 真实 HTTP 服务 + curl + CLI

在临时端口（18080 被本机其他服务占用，报 `OSError: [Errno 98]
Address already in use`，改用内核分配的空闲端口 57253）上实际启动：

```text
$ python3 -m tlog.cli serve --port 57253 --data-dir /tmp/tlog-rundemo
$ curl -s http://127.0.0.1:57253/health
{"status": "ok", "tree_size": 0}
$ BASE=http://127.0.0.1:57253 bash examples/requests.sh
（3 条叶子的完整 JSON 响应正常；STH 含合法 Ed25519 签名与公钥；
 包含证明 / 历史大小证明 / 一致性证明 / 叶子读取全部 200）
```

CLI 本地验证（退出码实测）：

```text
$ python3 -m tlog.cli --url $BASE sth          → 校验结果：通过 ✓（exit 0）
$ python3 -m tlog.cli --url $BASE inclusion 7  → 校验结果：通过 ✓（exit 0）
$ python3 -m tlog.cli --url $BASE inclusion 9 --expect-root 00…00
                                                → 校验结果：失败 ✗（exit 1）
$ python3 -m tlog.cli --url $BASE consistency 3→ 校验结果：通过 ✓（exit 0）
```

真实响应已保存到 `examples/sample_responses.json`。

## 5. 磁盘存储篡改实测

```text
# 手工把 /tmp/tlog-rundemo/leaves.jsonl 第 1 条叶子的 base64 改成别的内容
$ python3 -c "from tlog.log import Log; Log('/tmp/tlog-rundemo')"
CorruptLogError: 第 1 行记录哈希不匹配（叶子内容被篡改）
# 恢复原文件后
tree_size=10，重新加载成功
```

插入、删除记录的同类拒绝由自动化测试 `test_inserted_record_detected`、
`test_deleted_record_detected` 覆盖。

## 6. 开发过程中发现并修复的问题（如实记录）

### 6.1 真实 bug：二次幂分裂点公式错误（初次测试即发现，已修复）

初版 `_largest_pow2_less_than` 写成 `1 << (n.bit_length() - 1)`。
当 n 本身是二次幂时（如 n=2）返回 2 而不是严格小于 n 的 1，
导致 MTH 递归区间不收缩：

```text
RecursionError: maximum recursion depth exceeded
  File "tlog/merkle.py", line 50, in mth
    result = node_hash(mth(start, start + k), mth(start + k, end))
  [Previous line repeated 981 more times]
```

第一次跑测试时有 36 个用例因此 error（绝大多数树都含二次幂子树）。
修复为 `1 << ((n - 1).bit_length() - 1)`，修复后递归与 RFC 的
`k < n ≤ 2k` 完全一致，全部相关用例通过。这也验证了
「测试先行交叉验证」的必要性——锚点用例（空树、单叶）通过但
双叶树立即暴露了问题。

### 6.2 一次错误测试断言 → 实测澄清 RFC 验证器语义

我起初在「伪造树大小」测试里断言：对 m=3、真实 n=11 的证明，
谎报任意其他 tree_size 都应失败。实测并非如此：

```text
claimed_size= 1 .. 8  -> reject
claimed_size= 9 .. 16 -> PASS
claimed_size=17 .. 24 -> reject
```

核对 RFC 9162 §2.1.6.2 后确认：验证算法**不检查路径长度**，
只按 (fn, sn) 的最低位消费路径；树高为 4 时，9..16 内的位形相同，
重建出的正是真实 11 叶根。这是协议的已知语义（RFC 6962 的
验证器同样如此），安全依赖于「tree_size 与 root_hash 必须来自
已验签 STH」，而非随证明信任。已将测试改写为：

- `test_wrong_tree_size_rejected`：只断言真正被拒绝的大小（≤8、≥17）；
- `test_tree_size_must_come_from_signed_sth_not_trusted_with_proof`：
  显式固化 9..16 通过、且对 16 叶的另一个真实根必然失败的性质，
  文档中也写明了这一安全用法提醒。

### 6.3 其他小问题（当轮修复，无遗留）

- 初版 `log.py` 追加逻辑里残留了绕弯的 `__import__` 写法，重写干净；
- 初版 `cli.py` 误重复注册 `serve` 子命令、`client.py` 少了
  `urllib.error` 导入，重写/补齐；
- `scripts/demo.py` 直接运行时项目根不在 `sys.path`（ModuleNotFoundError），
  加了路径引导。

## 7. 未通过项 / 已知限制

- **无未通过的测试**：57/57 通过。
- 已知限制（设计如此，非遗留缺陷）：
  - 不解决日志分叉 / 运营者共谋（见 README「威胁模型」）；
  - 私钥 PEM 未加密落盘，仅本地测试用途；
  - 树根为 O(n) 惰性重算，未做持久化节点缓存，规模大时需优化；
  - 服务仅绑定 127.0.0.1，无鉴权、无 TLS——按「本地安全数据处理」
    范围有意为之，不应直接暴露到网络。
