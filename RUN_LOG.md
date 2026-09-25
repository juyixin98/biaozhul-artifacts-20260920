# 运行记录(实际执行,如实记录)

环境:Python 3.12.3,NumPy 2.5.3,pytest 9.1.1,Linux 6.8.0-90-generic。日期:2026-09-25。

## 1. 自动化测试

命令:

```
python3 -m pytest tests/ --cov=fir_stream --cov-report=term
```

结果:**23 passed in 0.64s**,行覆盖率 **89%**(core.py 94%, service.py 89%, cli.py 95%)。

```
tests/test_streaming_fir.py ......................                       [100%]
Name                     Stmts   Miss  Cover
--------------------------------------------
fir_stream/__init__.py       4      0   100%
fir_stream/analysis.py      24      5    79%
fir_stream/cli.py           19      1    95%
fir_stream/core.py         128      8    94%
fir_stream/filters.py       49     10    80%
fir_stream/pcm.py           24      4    83%
fir_stream/service.py       65      7    89%
fir_stream/synth.py         16      0   100%
--------------------------------------------
TOTAL                      329     35    89%
```

覆盖的验收点:

- 整段卷积参考比对:分块大小 1/3/7/64/256/1000/4096 及不整除分块,误差 < 1e-10;
- 切换中再切换:渐变中发起第二次切换,稳态精确等于最终滤波器参考,且无超界跳变;
- 通道数变化:2→3→1 重配置后输出等于全新引擎参考;
- 每通道隔离:单通道切换不影响其他通道(误差 < 1e-12);
- 空块不改变状态:状态快照逐元素一致;
- 边界不连续:流式输出与整段参考在块边界处的跳变量一致(< 1e-10),检测器能找出人为植入的跳变。

## 2. 开发中发现并修复的问题

首轮测试 19 通过 / 3 失败。失败根因:交叉渐变 ramp 在 positions 超过渐变长度后,`cos` 继续振荡使权重回落到 0(渐变压不住,旧 lane 重新混入)。修复:`_raised_cosine_ramp` 中先将 positions 钳位到 `total`,使渐变结束后权重恒为 1。修复后全部通过。

## 3. 示例作业实际运行

命令:

```
python3 -m fir_stream.cli examples/jobs/synth_crossfade.job.json
python3 examples/make_sample_pcm.py
python3 -m fir_stream.cli examples/jobs/pcm_file.job.json
```

结果(摘要,完整 JSON 见 `examples/jobs/report_*.json`):

- 合成信号作业:48000 样本进/出,189 块,3 次切换全部应用(含样本 16400 处的"切换中再切换"与样本 32000 处仅通道 0 的硬切换),`max_abs_output` 0.5099,输出 `examples/jobs/out_synth.f32`。
- PCM 文件作业:24000 样本进/出,47 块,1 次 512 样本交叉渐变切换,`max_abs_output` 0.6202,输出 `examples/jobs/out_pcm.f32`。

## 未通过项

无。所有 23 项测试通过,两个示例作业均成功运行。
