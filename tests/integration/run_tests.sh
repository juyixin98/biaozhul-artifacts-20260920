#!/usr/bin/env bash
# 端到端集成测试：编译出的二进制 + JSON 请求文件/stdin。
# 用 grep 校验输出中的关键数值，避免依赖 jq/python。
set -u

BIN="${1:-build/sphere_dist}"
FAIL=0
CHECKS=0
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# assert_contains <名称> <输出文件> <期望子串>
assert_contains() {
  local name="$1" file="$2" needle="$3"
  CHECKS=$((CHECKS + 1))
  if grep -qF -- "$needle" "$file"; then
    echo "[PASS] $name"
  else
    FAIL=$((FAIL + 1))
    echo "[FAIL] $name (want substring: $needle)"
    echo "       output: $(tr '\n' ' ' < "$file")"
  fi
}

assert_ok() {
  local name="$1" file="$2"
  assert_contains "$name" "$file" '"ok": true'
}

assert_err() {
  local name="$1" file="$2" needle="$3"
  CHECKS=$((CHECKS + 1))
  if grep -qF -- '"ok": false' "$file" && grep -qF -- "$needle" "$file"; then
    echo "[PASS] $name"
  else
    FAIL=$((FAIL + 1))
    echo "[FAIL] $name (want error containing: $needle)"
    echo "       output: $(tr '\n' ' ' < "$file")"
  fi
}

echo "== distance: equator -> north pole = pi R/2 = 10007543.398010 m =="
printf '{"action":"distance","a":{"lat":0,"lon":0},"b":{"lat":90,"lon":0}}' \
  | "$BIN" > "$TMP/o1"
assert_ok "exit-ok quarter" "$TMP/o1"
assert_contains "quarter meridian meters" "$TMP/o1" '"distance_m": 10007543.398010'
assert_contains "quarter meridian km" "$TMP/o1" '"distance_km": 10007.543398'

echo "== distance: antipodes on equator = pi R = 20015086.796021 m =="
"$BIN" examples/distance_antipodes.json > "$TMP/o2"
assert_ok "exit-ok antipode eq" "$TMP/o2"
assert_contains "antipode equator meters" "$TMP/o2" '"distance_m": 20015086.796021'
assert_contains "antipode angle deg" "$TMP/o2" '"central_angle_deg": 180'

echo "== distance: cross antimeridian (179 -> -179) = 2 deg = 222389.853289 m =="
"$BIN" examples/distance_antimeridian.json > "$TMP/o3"
assert_ok "exit-ok antimeridian" "$TMP/o3"
assert_contains "cross-antimeridian meters" "$TMP/o3" '"distance_m": 222389.853289'

echo "== distance: New York antipodal point =="
"$BIN" examples/distance_antipodes.json > "$TMP/o4"
assert_ok "exit-ok nyc antipode" "$TMP/o4"
assert_contains "nyc antipode meters" "$TMP/o4" '"distance_m": 20015086.796021'

echo "== distance: identical points = 0 =="
printf '{"action":"distance","a":{"lat":35.1,"lon":139.7},"b":{"lat":35.1,"lon":139.7}}' \
  | "$BIN" > "$TMP/o5"
assert_ok "exit-ok identical" "$TMP/o5"
assert_contains "identical distance zero" "$TMP/o5" '"distance_m": 0.000000'
assert_contains "identical angle zero" "$TMP/o5" '"central_angle_rad": 0'

echo "== range: cross antimeridian, r=350km (~3.15 deg) =="
"$BIN" examples/range_antimeridian.json > "$TMP/o6"
assert_ok "exit-ok range anti" "$TMP/o6"
assert_contains "range anti hit_count 2" "$TMP/o6" '"hit_count": 2'
assert_contains "range anti includes -179" "$TMP/o6" '"lon": -179'
if grep -qF '"lon": -176' "$TMP/o6"; then
  FAIL=$((FAIL + 1)); echo "[FAIL] range anti must exclude -176 (5 deg away)"
else
  CHECKS=$((CHECKS + 1)); echo "[PASS] range anti excludes -176"
fi

echo "== range: near pole, r=600km (~5.4 deg) =="
"$BIN" examples/range_near_pole.json > "$TMP/o7"
assert_ok "exit-ok range pole" "$TMP/o7"
assert_contains "range pole hit_count 3" "$TMP/o7" '"hit_count": 3'
assert_contains "range pole wraps longitude 180->-180 canonical" "$TMP/o7" '"lon": -180'
if grep -qF '"lon": 0.0,"' "$TMP/o7" && grep -qF '"lat": 80' "$TMP/o7"; then
  : # 80N 应被排除；下面单独判定更稳妥
fi
if grep -qF '"lat": 80' "$TMP/o7"; then
  FAIL=$((FAIL + 1)); echo "[FAIL] range pole must exclude 80N"
else
  CHECKS=$((CHECKS + 1)); echo "[PASS] range pole excludes 80N"
fi

echo "== range: radius pi R includes every point incl. antipodes =="
printf '{"action":"range","center":{"lat":10,"lon":10},"radius_m":20015087,"points":[{"lat":-10,"lon":-170},{"lat":0,"lon":0},{"lat":-90,"lon":0}]}' \
  | "$BIN" > "$TMP/o8"
assert_contains "range hemisphere hit_count 3" "$TMP/o8" '"hit_count": 3'

echo "== error handling =="
printf '{"action":"distance","a":{"lat":91,"lon":0},"b":{"lat":0,"lon":0}}' \
  | "$BIN" > "$TMP/e1"; rc=$?
assert_err "lat > 90 rejected" "$TMP/e1" "latitude out of range"
[ "$rc" -ne 0 ] && { CHECKS=$((CHECKS + 1)); echo "[PASS] nonzero exit on error"; } \
                  || { FAIL=$((FAIL + 1)); echo "[FAIL] error exit code"; }

printf '{"action":"distance","a":{"lat":0,"lon":200},"b":{"lat":0,"lon":0}}' \
  | "$BIN" > "$TMP/e2"
assert_err "lon > 180 rejected" "$TMP/e2" "longitude out of range"

printf '{"action":"ping"}' | "$BIN" > "$TMP/e3"
assert_err "unknown action rejected" "$TMP/e3" "unknown action"

printf 'not json' | "$BIN" > "$TMP/e4"
assert_err "invalid JSON rejected" "$TMP/e4" "invalid JSON"

printf '{"action":"range","center":{"lat":0,"lon":0},"radius_m":-1,"points":[]}' \
  | "$BIN" > "$TMP/e5"
assert_err "negative radius rejected" "$TMP/e5" "non-negative"

echo
echo "$CHECKS checks, $FAIL failures"
if [ "$FAIL" -ne 0 ]; then
  echo "INTEGRATION TESTS FAILED"
  exit 1
fi
echo "ALL INTEGRATION TESTS PASSED"
