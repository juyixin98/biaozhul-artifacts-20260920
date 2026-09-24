# RUNLOG — 验证命令与结果记录

记录时间:2026-09-24。环境:Linux 6.8.0-90-generic,Python 3.12.3,NumPy 2.5.3。
所有命令均在仓库根目录执行,输出为真实运行结果(未手工修饰)。

## 1. 自动化测试

命令:

```bash
python3 -m unittest discover -s tests -t . -v
```

结果:**66 个用例全部通过,0 失败,0 错误**(耗时约 0.06 s)。

```
Ran 66 tests in 0.058s
OK
```

用例分布:

| 模块 | 用例数 | 覆盖 |
|---|---|---|
| tests.test_acceptance | 5 | 验收场景:截断 chunk / 错误采样对齐 / 极值样本 / 往返字节 / 格式拒绝 |
| tests.test_pcm | 14 | 16/24 bit 编解码、符号扩展、极值映射、裁剪、NaN 拒绝、位深拒绝 |
| tests.test_wavio | 26 | 未知 chunk 跳过、奇数 pad、RIFF 长度、截断、对齐、格式拒绝、wave 模块互操作 |
| tests.test_service | 11 | validate/synthesize/roundtrip 请求处理 |
| tests.test_cli | 5 | CLI 退出码、JSON 请求(文件/stdin)、批量部分失败 |

关键断言摘录(均通过):

- `test_accept_1_truncated_chunks`:截断的 JUNK/fmt/data chunk 均抛
  `WavFormatError`,信息含 chunk id 与偏移。
- `test_accept_2_wrong_sample_alignment`:16bit 单声道 5 字节 data → 2 帧 +
  `PARTIAL_FRAME`;24bit 立体声 8 字节 data → 1 帧 + 丢弃 2 字节。
- `test_accept_3_extreme_samples`:`-32768↔-1.0`、`32767↔32767/32768`、
  `-8388608↔-1.0`、`8388607↔8388607/8388608` 精确互转;`+1.0` 量化为正满幅不翻转。
- `test_accept_4_roundtrip_bytes`:PCM 负载逐字节一致;干净文件整文件重写一致;
  RIFF 长度字段 == 文件长度 - 8(含奇数 pad)。
- `test_accept_5_reject_non_pcm_formats`:8/32/64 bit、IEEE float、AIFF、AVI 均拒绝。

## 2. 端到端示例

命令:

```bash
bash examples/run_examples.sh
```

完整输出存档于 `examples/EXAMPLE_OUTPUT.log`。各步骤退出码:

| 步骤 | 退出码 | 说明 |
|---|---|---|
| request synthesize_fullscale24.json | 0 | 24bit 满幅信号 → WAV + raw PCM,极值计数 8/8 |
| request validate_wav.json | 0 | 解析上一步产物,issues 为空 |
| request validate_raw.json | 0 | raw PCM 按 24bit 解码,统计一致 |
| request roundtrip_sine16.json | 0 | 三项 checks 全 true;量化误差 1.507e-05 ≤ 0.5 LSB (1.526e-05) |
| request batch.json | **1(预期)** | 4 条中第 4 条故意校验不存在文件 → 该条 `ok:false`,其余正常 |
| make_fixtures.py | 0 | 生成 6 个问题容器样本 |
| request validate_fixtures.json | **1(预期)** | 截断 JUNK / 8bit / float32 三条按设计被拒绝(ok:false);奇 pad JUNK、非帧对齐、RIFF 长度不符三条正常并带告警 |
| cli synth / validate / roundtrip | 0 | 子命令直连路径 |

上表两个 `exit=1` 是**故意构造的负例**,用于演示拒绝路径与逐条报错,
不是缺陷。批量请求的语义:部分条目失败时整体退出码为 1,每条结果独立给出
`ok`/`error`。

## 3. 开发过程中发现并已修复的问题(如实记录)

1. `unittest discover -s tests` 默认不以包方式导入,相对导入
   `from .helpers import ...` 失败 → 改用 `-t .` 并补 `tests/__init__.py`。
2. `service._roundtrip` 重构后残留未定义变量 `source_amp`(NameError,
   被 `test_synthetic_sine_24_*` 捕获)→ 改为直接使用 `source_f`。
3. 验收测试对告警文案断言过严(`byte` vs `byte(s)`)→ 改为子串断言。
4. 批量请求遇缺失文件时 `FileNotFoundError` 未被逐条捕获,整个批次以
   退出码 2 中断 → `request` 子命令增加 `OSError` 逐条捕获,行为变为
   退出码 1 + 该条 `ok:false`,并补 `tests/test_cli.py` 锁定。
5. 初版把"末尾不足 8 字节的残余"误判为 chunk 头截断错误 → 改为
   `TRAILING_BYTES` 告警(更贴近真实文件),相应测试同步调整。

## 4. 当前未通过项

无。全部 66 个测试通过;示例脚本中两个 `exit=1` 为上述预期负例。
