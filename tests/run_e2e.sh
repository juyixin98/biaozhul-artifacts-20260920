#!/usr/bin/env bash
# run_e2e.sh — 端到端测试：通过 CLI/JSON 入口驱动主程序
#   - 样例请求的基本校验（Python 解析输出、核对期望 id/顺序）
#   - 管道( stdin )与文件(-o)两种 IO 路径
#   - 随机请求：Python 独立全扫描参照实现逐字段交叉校验
#   - 非法请求的错误码与退出码
set -u
cd "$(dirname "$0")/.."

BIN=./build/spatial_index
PY=${PYTHON:-python3}
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
FAIL=0

say()  { printf '%s\n' "$*"; }
pass() { say "[PASS] $1"; }
fail() { say "[FAIL] $1"; FAIL=1; }

# 期望退出码与 JSON 字段的辅助
expect_ok() { # $1=输出文件 $2=描述
  if $PY -c '
import json,sys
d=json.load(open(sys.argv[1]))
assert d["ok"] is True, d
' "$1" 2>"$TMP/err"; then pass "$2"; else fail "$2: $(cat "$TMP/err")"; fi
}

expect_err() { # $1=输出文件 $2=期望 code $3=描述
  if $PY -c '
import json,sys
d=json.load(open(sys.argv[1]))
assert d["ok"] is False and d["error"]["code"]==sys.argv[2], d
' "$1" "$2" 2>"$TMP/err"; then pass "$3"; else fail "$3: $(cat "$TMP/err")"; fi
}

say "==== 样例请求 ===="
# 1) 文件入参 + stdout
$BIN examples/request_knn.json > "$TMP/knn.json"
[ $? -eq 0 ] && expect_ok "$TMP/knn.json" "knn 样例退出码0且 ok=true" || fail "knn 样例执行失败"
$PY - "$TMP/knn.json" <<'EOF' && pass "knn 样例顺序/截断标志正确" || fail "knn 样例结果不正确"
import json,sys
d=json.load(open(sys.argv[1]))
rs=d["results"]
# (0,0)：点1 d2=0，点7(0.5,-0.5) d2=0.5，点4(3.1,3.9) d2=24.82 先于点2的25
assert [h["id"] for h in rs[0]["results"]]==[1,7,4], rs[0]
assert abs(rs[0]["results"][2]["distance_sq"]-24.82)<1e-12
assert rs[2]["truncated"] is True and rs[2]["count"]==7
EOF

# 2) stdin 管道
cat examples/request_radius.json | $BIN > "$TMP/r.json"
[ $? -eq 0 ] || fail "radius stdin 执行失败"
$PY - "$TMP/r.json" <<'EOF' && pass "radius 样例边界包含/排序正确" || fail "radius 样例结果不正确"
import json,sys
d=json.load(open(sys.argv[1]))
rs=d["results"]
# radius=0 只含原点 15；radius=5 含全部点：d2 依次 0,2,2,8,25（点14恰在圆周）
assert [h["id"] for h in rs[0]["results"]]==[15], rs[0]
assert [h["id"] for h in rs[1]["results"]]==[15,11,12,13,14], rs[1]
assert rs[1]["results"][-1]["distance_sq"]==25.0
# 圆心(2,2) 半径1：仅点13（点13在圆心）
assert [h["id"] for h in rs[2]["results"]]==[13], rs[2]
assert rs[3]["count"]==5
EOF

# 3) -o 输出文件
$BIN examples/request_degenerate.json -o "$TMP/deg.json"
[ $? -eq 0 ] && [ -s "$TMP/deg.json" ] && pass "-o 写出文件成功" || fail "-o 写出失败"
$PY - "$TMP/deg.json" <<'EOF' && pass "退化样例：重复坐标/共线/极大坐标/负id" || fail "退化样例结果不正确"
import json,math,sys
d=json.load(open(sys.argv[1]))
rs=d["results"]
# (2,2) k=5：三个重合点 101,102,103 距离0；203(2,0) d2=4；204(4,0) d2=8
# （202(-2,0) 的 d2=20，不入选）
assert [h["id"] for h in rs[0]["results"]]==[101,102,103,203,204], rs[0]
# 查询圆心为 (0,0)、半径4：
# d2=4(恰在圆周) -> 202(-2,0),203(2,0)；d2=8 -> 101,102,103(2,2)；
# d2=16(圆周) -> 201(-4,0),204(4,0)
ids=[h["id"] for h in rs[1]["results"]]
assert ids==[202,203,101,102,103,201,204], ids
assert rs[2]["results"][0]["id"]==-7
# 极大坐标点不应参与近邻结果且所有输出距离有限
for q in rs:
    for h in q["results"]:
        assert math.isfinite(h["distance_sq"]), h
EOF

say ""
say "==== 随机数据：Python 全扫描独立交叉校验 ===="
$PY - "$BIN" <<'EOF'
import json, math, random, subprocess, sys
binp=sys.argv[1]
fails=0
def b128(v):
    # 与 C++ long double 无关：python float 即可，独立实现各自语义，
    # 交叉校验只针对普通 double 可精确往返的坐标（整数值网格+小随机）。
    return v
for seed in range(40):
    rnd=random.Random(seed)
    n=rnd.randint(0,400)
    mode=rnd.randint(0,2)
    pts=[]
    for i in range(n):
        if mode==0:   # 整数网格：制造重复/共线/并列
            x=float(rnd.randint(-12,12)); y=float(rnd.randint(-12,12))
        elif mode==1: # 共线
            x=float(rnd.randint(-50,50)); y=0.0
        else:
            x=round(rnd.uniform(-100,100),3); y=round(rnd.uniform(-100,100),3)
        pts.append({"id":i+1,"x":x,"y":y})
    qs=[]
    ref=[]
    for _ in range(6):
        if mode==1:
            qx=float(rnd.randint(-55,55)); qy=0.0
        else:
            qx=round(rnd.uniform(-30,30) if mode!=0 else rnd.randint(-13,13),3)
            qy=0.0 if mode==1 else round(rnd.uniform(-30,30) if mode!=0 else rnd.randint(-13,13),3)
        kind=rnd.randint(0,1)
        if kind==0:
            k=rnd.choice([0,1,3,n,n+5])
            qs.append({"type":"knn","x":qx,"y":qy,"k":k})
            order=sorted((( (p["x"]-qx)**2+(p["y"]-qy)**2, p["id"]) for p in pts))
            ref.append([i for _,i in order[:min(k,n)]])
        else:
            r=float(rnd.choice([0,1,5,10,20,1000]))
            qs.append({"type":"radius","x":qx,"y":qy,"radius":r})
            r2=r*r
            order=sorted((((p["x"]-qx)**2+(p["y"]-qy)**2, p["id"]) for p in pts))
            ref.append([i for d2,i in order if d2<=r2+1e-9])
    req={"points":pts,"queries":qs}
    out=subprocess.run([binp],input=json.dumps(req),capture_output=True,text=True)
    if out.returncode!=0:
        print("非零退出码 seed",seed,out.stdout[:300]); fails+=1; continue
    res=json.loads(out.stdout)
    for qi,(q,want) in enumerate(zip(qs,ref)):
        got=[h["id"] for h in res["results"][qi]["results"]]
        if got!=want:
            print(f"不一致 seed={seed} q={qi} type={q['type']}\n  want={want[:12]}\n  got ={got[:12]}")
            fails+=1
            break
print(f"随机交叉校验: {40} 个数据集, 失败 {fails}")
sys.exit(1 if fails else 0)
EOF
[ $? -eq 0 ] && pass "40 组随机请求与 Python 全扫描完全一致" || fail "随机交叉校验存在不一致"

say ""
say "==== 错误处理与退出码 ===="
echo '{ not json' | $BIN > "$TMP/e1.json" 2>/dev/null
[ $? -eq 1 ] && expect_err "$TMP/e1.json" INVALID_JSON "非法 JSON 退出码1"

$PY -c 'import json;print(json.dumps({"points":[]}))' | $BIN > "$TMP/e2.json" 2>/dev/null
expect_err "$TMP/e2.json" MISSING_FIELD "缺少 queries 报错"

$PY -c 'import json;print(json.dumps({"points":[{"id":1,"x":0,"y":0},{"id":1,"x":1,"y":1}],"queries":[]}))' | $BIN > "$TMP/e3.json" 2>/dev/null
expect_err "$TMP/e3.json" DUPLICATE_ID "重复 id 报错"

$PY -c 'import json;print(json.dumps({"points":[{"id":1,"x":0,"y":0}],"queries":[{"type":"knn","x":0,"y":0,"k":-1}]}))' | $BIN > "$TMP/e4.json" 2>/dev/null
expect_err "$TMP/e4.json" INVALID_TYPE "k=-1 报错"

$PY -c 'import json;print(json.dumps({"points":[{"id":1,"x":0,"y":0}],"queries":[{"type":"radius","x":0,"y":0,"radius":-2}]}))' | $BIN > "$TMP/e5.json" 2>/dev/null
expect_err "$TMP/e5.json" VALUE_OUT_OF_RANGE "负半径报错"

echo '{"points":[{"id":1,"x":1e2500,"y":0}],"queries":[]}' | $BIN > "$TMP/e6.json" 2>/dev/null
expect_err "$TMP/e6.json" VALUE_OUT_OF_RANGE "越界坐标报错"

$BIN /nonexistent/xxx.json >/dev/null 2>&1
[ $? -eq 1 ] && pass "输入文件不存在退出码1" || fail "文件存在性检查失败"

say ""
if [ $FAIL -eq 0 ]; then say "全部端到端测试通过 ✅"; else say "存在失败项 ❌"; fi
exit $FAIL
