#!/usr/bin/env bash
# SAM 命令行完整调用样例 (离线)。
# 直接在仓库根目录执行: bash examples/cli-commands.sh
set -euo pipefail

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
echo "工作目录: $WORK"

# 1) 本地生成 Ed25519 测试密钥 (私钥 0600; 不接入任何生产账户)
python3 -m sam keygen \
  --private-key "$WORK/keys/key.pem" \
  --public-key "$WORK/trust/key.pem"

# 2) 准备一个最小制品
mkdir -p "$WORK/artifact/bin" "$WORK/artifact/data"
printf "import sys\nprint('hello', sys.argv[1:])\n" > "$WORK/artifact/bin/app.py"
printf "payload\n" > "$WORK/artifact/data/msg.txt"

# 3) 离线签名 (封套必须落在制品根之外, 否则严格模式视为清单外文件)
python3 -m sam sign \
  --artifact-root "$WORK/artifact" \
  --private-key "$WORK/keys/key.pem" \
  --output "$WORK/envelope.sam.json" \
  --entrypoint bin/app.py

# 4) 离线验证 (人类可读)
python3 -m sam verify \
  --artifact-root "$WORK/artifact" \
  --envelope "$WORK/envelope.sam.json" \
  --trust-dir "$WORK/trust"

# 5) 离线验证 (机器可读 JSON)
python3 -m sam verify \
  --artifact-root "$WORK/artifact" \
  --envelope "$WORK/envelope.sam.json" \
  --trust-dir "$WORK/trust" --json

# 6) 先验证后执行 (验证失败绝不执行; -- 之后是透传给制品的参数)
python3 -m sam run \
  --artifact-root "$WORK/artifact" \
  --envelope "$WORK/envelope.sam.json" \
  --trust-dir "$WORK/trust" \
  --python -- demo-arg
