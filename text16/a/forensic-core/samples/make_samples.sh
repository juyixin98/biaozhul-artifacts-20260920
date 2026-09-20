#!/bin/sh
# 生成确定内容的小镜像样例（可重复生成，内容逐字节一致）。
set -eu
cd "$(dirname "$0")"

# sample-small.dd：64 KiB，确定性的伪随机内容
python3 - <<'EOF'
import random
r = random.Random(20260919)
with open("sample-small.dd", "wb") as f:
    f.write(bytes(r.randrange(256) for _ in range(64 * 1024)))
EOF

# sample-text.raw：可读的文本模式内容，32 KiB
i=0
: > sample-text.raw
while [ $i -lt 512 ]; do
    printf 'FORENSIC-SAMPLE block=%04d lorem-ipsum-dolor-sit-amet-%08d\n' "$i" "$((i * 7919))"
    i=$((i + 1))
done >> sample-text.raw

echo "samples generated:"
ls -l sample-small.dd sample-text.raw
sha256sum sample-small.dd sample-text.raw
