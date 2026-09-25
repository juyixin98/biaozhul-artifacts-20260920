# 运行记录 RUNLOG

环境：Linux 6.8.0，Python 3.12.3，cryptography 41.0.7，pytest 9.1.1（均为本机已有，
未接入任何生产账号；所有份额/密钥均为本地 CSPRNG 临时生成）。

## 最终结果

```bash
$ python3 --version
Python 3.12.3
$ python3 -c "import cryptography; print(cryptography.__version__)"
41.0.7
$ python3 -m pytest tests/ -q
72 passed in ~1s        # 连续运行 3 次结果一致（用于排除端口/时序偶发）
$ python3 -W error -m compileall -q threshold_shares tests
COMPILE_OK
$ bash examples/demo.sh   # 完整跑通：健康检查/切分/阈值恢复/少于阈值/混批/损坏编码/重复x
```

测试文件：`tests/test_field.py`、`test_scheme.py`、`test_encoding.py`、
`test_service.py`、`test_server.py`（活的 127.0.0.1 HTTP 服务，非 mock）。

详细逐条输出见 `examples/output/pytest-verbose.txt`；HTTP 演示真实输出见
`examples/output/demo-run.txt`。CLI 实际验证：3-of-5 切分后用 5 份恢复
（恰好使用 3 份，`used=[1,2,3], unused=[3,4]`），只给 2 份时退出码 2 并返回
`insufficient_shares`。

## 验收点对应

| 验收要求 | 位置 | 结果 |
|---|---|---|
| 任意阈值子集恢复 | `test_service.py::test_any_threshold_subset_recovers`（C(5,3)=10 组）、`test_scheme.py`（多 k/n + 15 组随机） | 通过 |
| 少于阈值拒绝接口恢复 | HTTP 400 `insufficient_shares`（`test_server.py`、demo.sh）；CLI rc=2 | 通过 |
| 混批份额 | 不同 k/n 与不同块数两例 `mixed_batch` | 通过 |
| 损坏编码处理 | 非规范 base64/截断/越界/超 p/未知版本/重复 x | 通过 |
| 检测重复横坐标 | `duplicate_share_index`，按位置报告；核心插值函数自身也拒绝 | 通过 |
| 参数与份额带版本 | `version=1`、`SSS1$` 前缀、未知版本/字段/flags 报错 | 通过 |
| 说明无认证份额无法指认恶意者 | 422 错误文案与 README §1；篡改 y + 指纹测试断言该措辞 | 通过 |

## 实现过程中实际出现并修复的失败（如实记录）

首轮测试 **33 failed / 39 passed**，逐项定位后修复的问题：

1. **块未按 31 字节边界填充** — 长度前缀+秘密直接切块，恢复时
   `to_bytes(31)` 重建出的布局错位，误报“长度后有非零数据”，并级联出
   OverflowError。修复：切分时对帧显式零填充（`scheme.py`）。
2. **随机系数取了整个 GF(p)（~256 bit），而块只有 248 bit** — 插值常数项可能
   ≥ 2²⁴⁸ 无法编码回 31 字节。修复：系数从 248 位块空间抽取，保持线性 Shamir
   对该空间封闭（`BLOCK_MOD = 2^248`）。
3. **拉格朗日公式符号错误（真实数学 bug）** — 实现成
   `prod x_j/(x_i-x_j)`，正确应为 `prod (0-x_j)/(x_i-x_j) = prod (-x_j)/(x_i-x_j)`。
   因 3-of-5 时全局符号差为 (-1)^(k-1)=+1，所有 3 阈值组合“恰好”通过，
   偶数阈值全部失败——这也是为什么测试矩阵必须覆盖 2-of-2、4-of-4 等组合。
   已修复并保留这些参数化用例做回归。
4. **口令封装头长度错误** — 把 5 字节 magic `SEAL1` 当 4 字节计算，salt/nonce
   切片整体错位，解封装报 bad magic。修复 `_HEADER_LEN` 与切片偏移。
5. **入口 `.strip()` 静默吞掉尾部换行** — 与“非规范 base64 必须拒绝”的契约冲突，
   已移除，要求输入严格规范。
6. **恢复时插值元素跑出块空间会抛 OverflowError 穿透服务** — 转为
   `recovered_secret_invalid` 结构化错误。
7. **413 请求体过大的测试偶发 URLError** — 服务器未读完 body 即关连接触发 RST；
   修复为先排空 body 再回 413，连跑 3 次稳定。
8. 若干测试侧问题（x 从 1 开始的断言、按概率构造 ≥p 元素改为全 0xFF 块、
   确定性的最后 y 块单 bit 翻转辅助 `conftest.flip_last_y`）。

修复后 72/72 通过。除上面列出的问题外，没有未通过项；没有跳过（skip）的测试。
