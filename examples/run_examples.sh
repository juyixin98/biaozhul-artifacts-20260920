#!/usr/bin/env bash
# 端到端演示：合成 -> 校验 -> 容器转换 -> raw 往返；再验证边界文件的拒绝行为。
# 所有输出落在 out/，stdout/stderr 原样保留。
set -u

cd "$(dirname "$0")/.."
mkdir -p out

echo "=== 1/4 合成 16 位正弦 + 24 位立体声扫频 ==="
python3 -m pcm_container run examples/01_synthesize_sine.json examples/02_synthesize_chirp.json

echo
echo "=== 2/4 容器校验（inspect）并导出 raw ==="
python3 -m pcm_container run examples/03_inspect_wav.json

echo
echo "=== 3/4 24 位 -> 16 位容器/位深转换 ==="
python3 -m pcm_container run examples/04_convert_24_to_16.json

echo
echo "=== 4/4 raw PCM 重新封装为 WAV ==="
python3 -m pcm_container run examples/05_raw_to_wav.json

echo
echo "=== 边界文件：生成 ==="
python3 examples/generate_edge_cases.py

echo
echo "=== 边界文件：合法极值/未知chunk/RIFF长度错误（退出码应为 0） ==="
for f in extremal_16.wav extremal_24.wav unknown_chunks.wav riff_size_wrong.wav; do
  echo "--- $f"
  python3 -m pcm_container run <(printf '{"action":"inspect_wav","input_wav":"out/edge_cases/%s"}' "$f") \
      && echo "[exit=0 OK]" || echo "[exit=$?]"
done

echo
echo "=== 边界文件：畸形/不支持（退出码应为 2） ==="
for f in truncated_chunk.wav bad_alignment.wav float32.wav pcm8.wav; do
  echo "--- $f"
  python3 -m pcm_container run <(printf '{"action":"inspect_wav","input_wav":"out/edge_cases/%s"}' "$f") 2>/dev/null
  code=$?
  if [ "$code" = "2" ]; then echo "[exit=2 已拒绝 OK]"; else echo "[exit=$code 非预期]"; fi
done
