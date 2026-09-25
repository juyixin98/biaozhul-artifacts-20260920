# 运行记录 RUNLOG

日期：2026-09-24
机器：Linux 6.8.0（Ubuntu），工作目录为仓库根。
所有命令均在无网络、无真实录音的条件下执行；输入信号全部由 NumPy 合成或手工字节构造。

## 环境

```text
$ python3 --version
Python 3.12.3
$ python3 -c "import numpy; print(numpy.__version__)"
2.5.3
```

## 1. 自动化测试

命令（仓库根目录，两种方式均验证过）：

```bash
python3 -m unittest discover -s tests -v
python3 -m pytest tests -q
```

结果：

```text
Ran 59 tests in 0.556s
OK          # unittest：59/59 通过
59 passed in 0.78s   # pytest：59/59 通过
```

完整逐用例输出见 `tests_results.log`。覆盖内容：

- 振幅转换（8 用例）：16/24 位码域边界、正负满量程、舍入、裁剪、NaN/Inf、往返恒等、其它位深拒绝；
- 合成信号（9 用例）：整帧对齐、非整帧拒绝、正弦精确取值、方波/锯齿值域、扫频、噪声确定性；
- WAV 读写（30 用例）：逐字节往返、立体声 24 位、未知 chunk 透传、
  截断 chunk 头/体、内部缺 pad / 文件尾缺 pad、data 不对齐、
  fmt block align 与 byte rate 矛盾、RIFF 长度篡改与尾部杂散字节、
  RIFF/WAVE 标识错误、float/8 位/32 位拒绝、缺/重复 fmt/data；
- 服务与 CLI（12 用例）：五类 action 端到端、文件产物、降位深越域计数、
  raw 往返、重采样拒绝、退出码 0/1/2。

## 2. 端到端样例

```bash
rm -rf out
bash examples/run_examples.sh
# 退出码 0；完整输出见 examples_run.log
```

流程与关键数值（摘录）：

1. `synthesize` 16 位 440Hz 正弦（48000 帧）：`rms=0.35355339...`、
   `min=-0.5, max=0.5`、`clipped_samples=0`；
   24 位立体声 200→4000Hz 扫频（96000×2 样本）写出成功。
2. `inspect_wav` 校验并导出 raw/npy。
3. `convert` 24→16 位：输出码域 [-29491, 29491]（= 0.9×32767 邻域），
   正常振幅无越域样本。
4. `wav_to_raw` → `raw_to_wav`：导出 raw 长度 96000 字节，
   与原 WAV 内 data chunk 体**逐字节相同**；重封装后全部帧样本一致
   （脚本外另做核验：`raw == data chunk body: True`，
   `frames equal: True`）。

## 3. 验收边界文件（合成构造，非真实录音）

`python examples/generate_edge_cases.py` 生成 8 个文件，校验结果：

| 文件 | 期望 | 实际 |
|---|---|---|
| extremal_16.wav（-32768/0/32767） | 接受，min=-1.0 | exit 0，min=-1.0，max=0.9999694824 |
| extremal_24.wav（-8388608/0/8388607） | 接受，min=-1.0 | exit 0，min=-1.0，max=0.9999998808 |
| unknown_chunks.wav（含奇数长度 chunk） | 跳过 JUNK/labl/note | exit 0，`extra_chunks=['JUNK','labl','note']` |
| riff_size_wrong.wav（声明 55，实际 48） | 接受但标记不匹配 | exit 0，`riff_size_matches_file=false` |
| truncated_chunk.wav（chunk 体截断） | 拒绝 | **exit 2 WavTruncatedError** |
| bad_alignment.wav（data 9 字节 / block 2） | 拒绝 | **exit 2 PcmAlignmentError** |
| float32.wav（wFormatTag=3） | 拒绝 | **exit 2 WavFormatError** |
| pcm8.wav（8 位 PCM） | 拒绝 | **exit 2 WavFormatError** |

错误输出示例（stderr，JSON）：

```json
{"ok": false, "error_type": "PcmAlignmentError",
 "error": "data 长度 9 字节不是 block align 2 (1 声道 x 16 位) 的整数倍，采样无法对齐"}
```

## 4. 开发过程中出现过、已修复的问题（如实记录）

首轮运行测试时**并非一次通过**，共发现并修复 3 处缺陷，修复后 59/59 全绿：

1. `parse_wav` 未处理 RIFF 声明尺寸之外的尾部杂散字节，导致
   `riff_size_wrong` 类文件在尾部 2 字节上误报"chunk 头截断"。
   修复：chunk 解析终点取 `min(文件尾, RIFF声明边界)`，1 字节内残留按
   pad 宽容处理；越界体同时对照文件尾与 RIFF 边界判定截断。
2. 一个测试辅助构造的 8 位 WAV 手工算错 RIFF 尺寸（先以截断路径命中），
   修正测试夹具后稳定命中 WavFormatError。
3. 一处测试断言把 48000Hz×0.01s 误写成 4800Hz（48 帧）却断言 480 帧；
   为保持断言语义改为 48000Hz。生产代码无此问题。

另外两处代码整洁性修正：移除 `wavio.py` 中遗留的 walrus 占位表达式、
`service.py` 中一版用 Python `or/and` 混合 NumPy 掩码的错误裁剪计数
（改为显式浮点舍入带比较，结果在 `clipped_samples` 字段输出）。

## 5. 未通过项 / 已知限制

- 无未通过的测试或验收项（59/59 + 8 个边界文件行为全部符合预期）。
- 有意拒绝而非缺陷：重采样请求（`output_sample_rate` 与源不同）、
  WAVE_FORMAT_EXTENSIBLE(0xFFFE)、32 位整数、8 位、float、ADPCM。
  这些属于本服务声明的 PCM 子集之外。
