#!/usr/bin/env bash
# 本地可信设置: 编译电路 + powers of tau + groth16 zkey。
# 仅用于本地开发/测试, 生产环境必须替换为多方安全计算 (MPC) 仪式, 见 README。
set -euo pipefail
cd "$(dirname "$0")/.."

BUILD=build
CIRCUIT=eligibility
POT_POWER=13   # 电路约 5.2k 约束, 2^13 = 8192 足够
mkdir -p "$BUILD"

entropy() { head -c 64 /dev/urandom | od -An -tx1 | tr -d ' \n'; }

echo "==> [1/4] 编译电路"
if [ ! -f "$BUILD/$CIRCUIT.r1cs" ]; then
  npx circom2 "circuits/$CIRCUIT.circom" --r1cs --wasm --sym \
    -l node_modules/circomlib/circuits -o "$BUILD"
else
  echo "    已存在, 跳过"
fi

echo "==> [2/4] powers of tau (bn128, 2^$POT_POWER)"
if [ ! -f "$BUILD/pot_final.ptau" ]; then
  npx snarkjs powersoftau new bn128 "$POT_POWER" "$BUILD/pot_0.ptau" -v
  npx snarkjs powersoftau contribute "$BUILD/pot_0.ptau" "$BUILD/pot_1.ptau" \
    --name="local-dev-contribution" -e="$(entropy)"
  npx snarkjs powersoftau prepare phase2 "$BUILD/pot_1.ptau" "$BUILD/pot_final.ptau" -v
  rm -f "$BUILD/pot_0.ptau" "$BUILD/pot_1.ptau"
else
  echo "    已存在, 跳过"
fi

echo "==> [3/4] groth16 电路专用设置"
if [ ! -f "$BUILD/${CIRCUIT}_final.zkey" ]; then
  npx snarkjs groth16 setup "$BUILD/$CIRCUIT.r1cs" "$BUILD/pot_final.ptau" \
    "$BUILD/${CIRCUIT}_0.zkey"
  npx snarkjs zkey contribute "$BUILD/${CIRCUIT}_0.zkey" "$BUILD/${CIRCUIT}_1.zkey" \
    --name="local-dev" -e="$(entropy)"
  npx snarkjs zkey beacon "$BUILD/${CIRCUIT}_1.zkey" "$BUILD/${CIRCUIT}_final.zkey" \
    0102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f 10 \
    -n="local-dev-beacon"
  rm -f "$BUILD/${CIRCUIT}_0.zkey" "$BUILD/${CIRCUIT}_1.zkey"
else
  echo "    已存在, 跳过"
fi

echo "==> [4/4] 导出验证密钥"
npx snarkjs zkey export verificationkey "$BUILD/${CIRCUIT}_final.zkey" \
  "$BUILD/verification_key.json"

echo "设置完成: $BUILD/${CIRCUIT}_final.zkey, $BUILD/verification_key.json"
