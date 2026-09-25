# 运行记录（RUN_LOG）

本文件如实记录在交付环境中的实际命令与结果。环境：

- OS：Linux 6.8.0-90-generic（Ubuntu），目录 `/home/admin/Downloads/biaozhul/opp97/b`
- Python：3.12.3（`/usr/bin/python3`）
- 依赖：`cryptography` 41.0.7、`pytest` 9.1.1（环境中已安装；见 `requirements.txt`）
- 记录时间：2026-09-24

## 1. 自动化测试

命令：

```bash
python3 -m pytest tests/ -v
python3 -m pytest tests/ -q
```

最终结果（最近两次运行）：

```
............................................                             [100%]
44 passed in 19.68s
```

44 个测试全部通过，退出码 0。测试文件与覆盖点：

| 文件 | 数量 | 覆盖 |
|---|---|---|
| `tests/test_rfc_vectors.py` | 11 | RFC 9162 §2.1.5 七叶符号例子（包含/一致性证明逐项）；RFC 6962 附录 8 叶二进制向量（空根、d(0) 叶哈希、大小 1–8 根、索引 0/6 包含证明、3→8 非二次幂一致性）；域分离 |
| `tests/test_merkle_properties.py` | 11 | 穷尽包含证明（树 1..300 × 每叶）、穷尽一致性（全部 (旧,新) 对 1..129）、二次幂/非二次幂旧大小、伪造根、错误索引、翻位/截断/加长证明、平凡情形 |
| `tests/test_store.py` | 6 | JSONL 追加与重启重放、历史根稳定、越界、超大条目、损坏/非连续文件检测 |
| `tests/test_signing.py` | 6 | Ed25519 本地生成/持久化、0600 权限、签名往返、篡改 size/root/timestamp/签名/换公钥全部失败 |
| `tests/test_log_service.py` | 5 | 服务层端到端、历史 STH(13) 包含、一致性、越界拒绝、旧根长期有效 |
| `tests/test_http_api.py` | 6 | 真实 HTTP（临时端口）完整工作流、STH 本地验签、伪造根/错误索引网络侧拒绝、一致性篡改、400/404、公钥端点 |

### 非二次幂叶数覆盖

- 小树逐项：RFC 七叶例子（7，非二次幂）+ 八叶二进制向量（8，二次幂）。
- 穷尽：所有树大小 1..300 的每个叶子索引（自动覆盖 3、5、6、7、9、…、300）。
- 一致性：1..129 全部 `(old,new)` 对，外加 100 叶日志上旧大小
  1,2,3,4,7,8,50,64,99 的独立脚本复核（见下）。

### 伪造旧根 / 错误索引 覆盖

- 包含证明：错误索引（含越界、负值）、伪造根（全 0）、异大小根、
  证明首/末元素翻位、截断、加长、元素长度错误 → 全部 `False`。
- 一致性：伪造旧根（全 0x11）、错误旧大小（m−1）、篡改首元素、加长证明 → 全部 `False`。
- STH：伪造根、改 tree_size、改时间戳、翻转签名字节、换另一把公钥 → 全部验签失败。

## 2. 端到端演示脚本

命令：`python3 examples/demo.py`
结果：退出码 0。要点输出：

```
== 2. signed tree head ==
  tree_size   = 7
  root_hash   = 9139601cc1ca8ab2a7a0c2c134c04845f2b1ba549a83d6c845cfcda439cc585d
  signature verifies: True (ok)
== 3. inclusion proofs, verified locally ==
  leaf 0..5: proof_len=3 verified=True ; leaf 6: proof_len=2 verified=True
== 4. append 5 more; consistency 7 -> 12 (non-power-of-two old size) ==
  consistency proof len=5 verified=True
== 5. attacker attempts (all must be rejected) ==
  forged root / wrong index / flipped bit / truncated proof / forged old root
    -> rejected (good)
```

## 3. 真实 HTTP 服务

启动（后台）：

```bash
python3 -m tl.server --host 127.0.0.1 --port 18088 --data-dir /tmp/tl-run/data
```

样例：`BASE=http://127.0.0.1:18088 bash examples/requests.sh`
结果：退出码 0。实测响应摘录：

- `GET /health` → `{"status":"ok","tree_size":0}`（追加后为 3）
- `POST /v1/entries` 文本与 base64 两种入参均返回递增 `index` 与 `leaf_hash`
- `GET /v1/sth` 返回 `tree_size/root_hash/timestamp_us/signature`，签名算法 Ed25519
- `GET /v1/proof/inclusion?leaf_index=1`（n=3，非二次幂）返回 2 个兄弟哈希
- `leaf_index=0&tree_size=1` 历史大小返回空证明，根等于叶哈希
- `/v1/proof/consistency?old_size=2` 与 `old_size=1&new_size=3` 均返回可核验证明
- `POST /v1/verify` 对真实包含证明返回 `{"verified":true}`
- 越界 `leaf_index=999` → HTTP 400；`/v1/entries?index=999` → HTTP 404

客户端独立验签/核验（`tl.client`，不信任服务端结论）：

```
STH signature locally verified: True
forged STH over HTTP: {'verified': False, 'reason': '... InvalidSignature'}
tampered inclusion over HTTP: {'verified': False}
genuine inclusion local verify: True
```

## 4. 持久化与重启

向运行中的服务追加 3 条后，磁盘文件：

```
ed25519_test_key.bin  -rw------- (0600, 32B)
log.jsonl             3 行 JSON：{"index","data_b64","ts"}
```

杀掉进程并用同一 `--data-dir` 重启：

```
GET /health -> {"status":"ok","tree_size":3}
public_key  重启前后相同：RcKRsALCk0Bi4jL12EgiAy7MsF9UO4hb35XwR/6F44c=
consistency 2->3 locally_verified: True
```

历史条目、根与密钥在重启后一致。

## 5. 边界与非二次幂额外脚本

独立脚本（进程内）对 100 叶日志复核：

```
inclusion at 16 sizes incl. non-powers: True
   （大小 1,2,3,6,7,8,9,15,16,17,31,63,64,65,99,100）
consistency old sizes 1,2,3,4,7,8,50,64,99 ->100: True
size-1 empty proof verifies: True len 0
m==n proof empty & roots equal: True
```

编译检查：`python3 -m py_compile tl/*.py tests/*.py examples/demo.py` → 无错误。

## 6. 开发过程中出现并修复的缺陷（如实记录）

以下问题在编写阶段通过测试发现并修复，**最终测试套件全部通过，无未通过项**：

1. 树分割函数差一错误：对二次幂 n（如 4）最初返回 k=4 而非严格小于 n 的
   最大 2 的幂（2），导致证明递归不收敛（RecursionError）。已修正
   `_largest_power_of_two_leq`，二次幂返回 n//2。
2. 包含证明验证器的兄弟哈希消费顺序/左右方向最初写反，导致自造证明被拒；
   修正为与生成器 PATH() 递归对偶（先深入、回程消费，并按半区决定左右连接）。
3. 一致性验证器对“旧大小为 2 的幂”的种子处理最初把预置旧根错误地放入哈希
   迭代器被消耗；改为以验证方持有的旧根直接初始化累加器，并加精确长度校验。
4. 测试脚本中的变量名遮蔽（如 `b`/`l`/`h`/`j` 与 Python 名字冲突）及对
   RFC 图中 `i,j,l` 节点层级的误读，曾造成测试侧假失败；经核对 RFC 9162
   §2.1.5 树图（`j` 为 d6 单叶、`l=H(i‖j)` 为 d4..d6 根）后更正测试，
   实现本身与 RFC 文字给出的证明列表一致。
5. 单叶已知向量最初凭记忆写错：测试误用输入 `b"\x00"`（单字节）却期待
   RFC 数据 `d(0)=0x00||0x00`（两字节）的发布值。实现输出
   `leaf_hash(b"\x00")=96a296d2…=SHA256(0x00 0x00)` 本身正确；发布向量
   对应输入是两字节 `d(0)`，其叶哈希为 `709e80c8…`。更正测试，统一采用
   RFC 6962/CT 二进制数据 `d(j)=0x00||j`（叶 d(0)=`709e80c8…`、
   n=8 根=`0a2a2c47…`）断言。

## 7. 未通过项 / 限制

- 自动化测试、演示脚本、HTTP 样例、持久化重启、编译检查：**无未通过项**。
- 已知功能边界（非缺陷，需求明确排除）：不解决日志分叉与共谋，无 gossip/
  见证者/法定人数/MMD；默认仅绑 127.0.0.1；密钥为本地测试密钥。详见 README「安全边界」。
