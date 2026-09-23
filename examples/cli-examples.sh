#!/usr/bin/env bash
# extsort CLI —— 典型用法与验收场景样例
#
# 直接运行：bash examples/cli-examples.sh
set -euo pipefail

# Resolve the repository root from this script's location so the demo works no
# matter the current directory it is launched from.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
BIN="${BIN:-$REPO_ROOT/target/release/extsort}"
if [[ ! -x "$BIN" ]]; then
  echo "building release binary first…"
  (cd "$REPO_ROOT" && cargo build --release)
fi

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
cd "$WORK"

say() { printf '\n==== %s ====\n' "$*"; }

say "0) 构建（仓库根目录执行一次即可）"
echo "cargo build --release"

say "1) 造数据：2 万行复合列 CSV（约 350 KiB）"
python3 - gen.csv <<'PY'
import random, sys
random.seed(2026)
with open("gen.csv", "w") as f:
    for i in range(20000):
        g = random.randrange(30)
        n = "".join(random.choice("abcdefg") for _ in range(random.randint(3, 12)))
        f.write(f"{g:02},{n},{i}\n")
PY
wc -l -c gen.csv

say "2) 极小内存（160 KiB）+ 2 车道，强制多轮归并；保留中间层"
"$BIN" sort --input gen.csv --output sorted.csv \
  --root ./repo --spec "1:asc,2:asc" --budget 163840 --lanes 2 --keep-temp
echo "中间分段层级："
find repo -name '*.run' | grep -oE 'level[0-9]+' | sort | uniq -c

say "3) 与内存参考排序对照（Python 稳定排序，tie 用第 3 列输入序号）"
python3 - gen.csv ref.csv <<'PY'
import sys
inp, outp = sys.argv[1], sys.argv[2]
rows = [l.rstrip("\n") for l in open(inp) if l.strip()]
idx = list(range(len(rows)))
idx.sort(key=lambda i: (rows[i].split(",")[0], rows[i].split(",")[1], i))
with open(outp, "w") as f:
    for i in idx:
        f.write(rows[i] + "\n")
PY
if cmp -s sorted.csv ref.csv; then echo "MATCH: 外排序 == 内存参考（稳定）"; else
  echo "MISMATCH"; diff <(head sorted.csv) <(head ref.csv) | head; exit 1
fi

say "4) 空输入"
: > empty.csv
"$BIN" sort --input empty.csv --output empty.out --root ./r_empty --budget 163840
echo "empty.out 字节数 = $(stat -c%s empty.out)（应为 0）"

say "5) 超大单行：300 KiB 一行，键是第 1 列（value 流式、不进内存）"
python3 - giant.csv <<'PY'
with open("giant.csv", "w") as f:
    f.write("k1," + "x" * 300_000 + "\n")
    f.write("zz,second\n")
PY
"$BIN" sort --input giant.csv --output giant.out \
  --root ./r_giant --spec "1:asc" --budget 163840 --lanes 2
python3 - giant.out <<'PY'
import sys
b = open(sys.argv[1], "rb").read()
first, second, tail = b.split(b"\n")
assert first == b"k1," + b"x" * 300_000, "giant bytes mismatch"
assert second == b"zz,second" and tail == b"", "order/newline mismatch"
print("GIANT OK: 300003 字节首行无损且排序正确")
PY

say "6) 故障注入 + 恢复（map 阶段第 9 段创建失败，随后 resume）"
set +e
"$BIN" sort --input gen.csv --output /dev/null \
  --root ./rf --job J --spec "1:asc,2:asc" --budget 163840 --lanes 2 --keep-temp \
  --fault "create_fail:run-00008"
echo "首次运行预期失败，退出码 = $?"
set -e
"$BIN" resume --root ./rf --job J --output recovered.csv
cmp -s recovered.csv ref.csv && echo "RECOVERY MATCH: 恢复结果 == 内存参考"

say "7) 临时段损坏检测（翻转一个已提交段的字节）"
# 重新停在 map 中段，破坏一个已提交 run，再 resume，应报 corrupt_segment
set +e
"$BIN" sort --input gen.csv --output /dev/null \
  --root ./rc --job C --spec 1 --budget 163840 --lanes 2 --keep-temp \
  --fault "create_fail:run-00008" >/dev/null 2>&1
RUN=$(find ./rc/jobs/C/level0 -name 'run-00003.run')
python3 - "$RUN" <<'PY'
import sys
p = sys.argv[1]; d = bytearray(open(p, "rb").read()); d[120] ^= 0xFF
open(p, "wb").write(d)
PY
"$BIN" resume --root ./rc --job C 2>&1 | head -1 || true
set -e

echo
echo "全部 CLI 样例完成。"
