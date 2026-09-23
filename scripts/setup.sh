#!/usr/bin/env bash
#
# Compile the circuit and run a SINGLE-MACHINE, TEST-ONLY Groth16 trusted setup.
#
# !!! DO NOT USE THESE KEYS IN PRODUCTION !!!
# The powers-of-tau ceremony here is contributed by one party on one machine.
# A real deployment needs a multi-party ceremony (phase 1, e.g. Hermez/trusted
# setup ceremony) plus a phase-2 ceremony for this exact circuit, and the
# resulting .zkey must be audited. A compromised toxic waste lets an attacker
# forge unlimited valid proofs. See README "可信设置与隐私边界".
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

CIRCUIT_NAME="eligibility"
PTAU_SIZE=15                          # supports up to 2^15 = 32768 constraints
BUILD_DIR="build"
PTAU_DIR="$BUILD_DIR/ptau"
PTAU_FILE="$PTAU_DIR/pot${PTAU_SIZE}_final.ptau"

mkdir -p "$PTAU_DIR"

# Local "entropy" for deterministic, reproducible test artifacts.
# This is NOT randomness anyone should trust — the whole point is demoing the
# cryptography locally.
PHASE1_ENTROPY="phase1-local-test-entropy-not-secret-0001"
PHASE2_ENTROPY_1="phase2-contributor-1-local-test-entropy-not-secret"
PHASE2_ENTROPY_2="phase2-contributor-2-local-test-entropy-not-secret"

echo "==> [1/6] Compiling circuit (r1cs + wasm + sym)"
./bin/circom circuits/${CIRCUIT_NAME}.circom \
  --r1cs --wasm --sym \
  -l node_modules \
  -o "$BUILD_DIR"

echo "==> [2/6] Circuit info (constraint count)"
npx snarkjs r1cs info "$BUILD_DIR/${CIRCUIT_NAME}.r1cs"

echo "==> [3/6] Phase 1 setup: powers of tau (size 2^${PTAU_SIZE})"
if [ -f "$PTAU_FILE" ]; then
  echo "    reusing existing $PTAU_FILE"
else
  npx snarkjs powersoftau new bn128 "$PTAU_SIZE" "$PTAU_DIR/pot0000.ptau" -v
  npx snarkjs powersoftau contribute "$PTAU_DIR/pot0000.ptau" "$PTAU_DIR/pot0001.ptau" \
    --name="single-machine test contribution" -e="$PHASE1_ENTROPY"
  npx snarkjs powersoftau prepare phase2 "$PTAU_DIR/pot0001.ptau" "$PTAU_FILE" -v
fi

echo "==> [4/6] Phase 2 setup (circuit-specific), Groth16"
npx snarkjs groth16 setup "$BUILD_DIR/${CIRCUIT_NAME}.r1cs" "$PTAU_FILE" "$BUILD_DIR/${CIRCUIT_NAME}_0000.zkey"
npx snarkjs zkey contribute "$BUILD_DIR/${CIRCUIT_NAME}_0000.zkey" "$BUILD_DIR/${CIRCUIT_NAME}_0001.zkey" \
  --name="1st test contributor" -e="$PHASE2_ENTROPY_1"
npx snarkjs zkey contribute "$BUILD_DIR/${CIRCUIT_NAME}_0001.zkey" "$BUILD_DIR/${CIRCUIT_NAME}_final.zkey" \
  --name="2nd test contributor" -e="$PHASE2_ENTROPY_2"

echo "==> [5/6] Applying a beacon contribution & exporting verification key"
npx snarkjs zkey beacon "$BUILD_DIR/${CIRCUIT_NAME}_final.zkey" "$BUILD_DIR/${CIRCUIT_NAME}_beacon.zkey" \
  0102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f20 10 -n="final test beacon"
npx snarkjs zkey verify "$BUILD_DIR/${CIRCUIT_NAME}.r1cs" "$PTAU_FILE" "$BUILD_DIR/${CIRCUIT_NAME}_beacon.zkey"
npx snarkjs zkey export verificationkey "$BUILD_DIR/${CIRCUIT_NAME}_beacon.zkey" "$BUILD_DIR/verification_key.json"

# Use the beacon zkey as the proving key
cp "$BUILD_DIR/${CIRCUIT_NAME}_beacon.zkey" "$BUILD_DIR/${CIRCUIT_NAME}.zkey"

echo "==> [6/6] Exporting Solidity verifier (optional, for reference)"
npx snarkjs zkey export solidityverifier "$BUILD_DIR/${CIRCUIT_NAME}.zkey" "$BUILD_DIR/Verifier.sol"

echo ""
echo "Setup complete. Artifacts in $BUILD_DIR/:"
echo "  ${CIRCUIT_NAME}.r1cs / ${CIRCUIT_NAME}_js/${CIRCUIT_NAME}.wasm — constraint system + witness generator"
echo "  ${CIRCUIT_NAME}.zkey      — proving key (TEST ONLY)"
echo "  verification_key.json     — verification key"
echo "  Verifier.sol              — on-chain verifier reference (not deployed)"
