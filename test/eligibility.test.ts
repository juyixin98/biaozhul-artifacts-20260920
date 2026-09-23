import { test, before, after } from "node:test";
import assert from "node:assert/strict";
import * as fs from "fs";
import * as path from "path";
import {
  buildCircuitInput,
  buildTree,
  computeNullifier,
  Member,
} from "../src/eligibility";
import { generateProof, ProofBundle } from "../src/prover";
import { NullifierRegistry, Verifier, verifyProof } from "../src/verifier";
import { poseidonHash } from "../src/poseidon";

const BUILD_DIR = path.join(__dirname, "..", "build");
const WASM_PATH = path.join(BUILD_DIR, "eligibility_js", "eligibility.wasm");
const ZKEY_PATH = path.join(BUILD_DIR, "eligibility_final.zkey");
const VKEY_PATH = path.join(BUILD_DIR, "verification_key.json");

const ACTIVITY = "2026092201";

const MEMBERS: Member[] = [
  { age: 34, secret: "19283746501234567890123456789012345678" },
  { age: 22, secret: "56473829105647382910564738291056473829" },
  { age: 51, secret: "10293847561029384756102938475610293847" },
  { age: 19, secret: "88776655443322110099887766554433221100" },
];

let vkey: any;
let root: string;

// snarkjs/ffjavascript 的 worker 线程在 Node 18 下会残留句柄, 阻止进程退出。
// 测试结果已通过 TAP 流上报给 node --test 运行器 (由运行器判定通过/失败),
// 这里在全部用例结束后强制结束测试子进程。
after(() => {
  setTimeout(() => process.exit(0), 200);
});

before(async () => {
  for (const p of [WASM_PATH, ZKEY_PATH, VKEY_PATH]) {
    assert.ok(fs.existsSync(p), `缺少 ${p}, 请先运行 npm run setup`);
  }
  vkey = JSON.parse(fs.readFileSync(VKEY_PATH, "utf8"));
  root = (await buildTree(MEMBERS)).root;
});

async function proveFor(
  members: Member[],
  index: number,
  activityId: string
): Promise<ProofBundle> {
  const input = await buildCircuitInput(members, index, activityId);
  return generateProof(input as unknown as Record<string, unknown>, WASM_PATH, ZKEY_PATH);
}

test("合法证明: 真实生成并通过 groth16 验证", async () => {
  const { proof, publicSignals } = await proveFor(MEMBERS, 1, ACTIVITY);
  assert.equal(publicSignals.length, 3);
  assert.equal(publicSignals[0], root, "公开输入[0] 应为 Merkle 根");
  assert.equal(publicSignals[1], ACTIVITY, "公开输入[1] 应为活动 ID");
  assert.equal(await verifyProof(vkey, publicSignals, proof), true);
});

test("年龄边界: 18 与 120 均可证明", async () => {
  const edge: Member[] = [
    { age: 18, secret: "11111111111111111111111111111111111111" },
    { age: 120, secret: "22222222222222222222222222222222222222" },
  ];
  for (const i of [0, 1]) {
    const { proof, publicSignals } = await proveFor(edge, i, ACTIVITY);
    assert.equal(await verifyProof(vkey, publicSignals, proof), true);
  }
});

test("年龄越界: 17 与 121 无法生成证明 (见证不满足约束)", async () => {
  for (const age of [17, 121, 0, 200]) {
    const members: Member[] = [{ age, secret: "33333333333333333333333333333333333333" }];
    const input = await buildCircuitInput(members, 0, ACTIVITY);
    await assert.rejects(
      generateProof(input as unknown as Record<string, unknown>, WASM_PATH, ZKEY_PATH),
      /Assert Failed|Error in template/i,
      `age=${age} 应当在见证生成阶段失败`
    );
  }
});

test("错误 Merkle 路径: 篡改路径元素后无法生成证明", async () => {
  const input = await buildCircuitInput(MEMBERS, 1, ACTIVITY);
  input.pathElements[3] = "999999999999999999999999";
  await assert.rejects(
    generateProof(input as unknown as Record<string, unknown>, WASM_PATH, ZKEY_PATH),
    /Assert Failed|Error in template/i
  );
});

test("非成员: 秘密不在树中无法生成证明", async () => {
  const input = await buildCircuitInput(MEMBERS, 1, ACTIVITY);
  input.secret = "99988877766655544433322211100099"; // 不在名单中的秘密
  await assert.rejects(
    generateProof(input as unknown as Record<string, unknown>, WASM_PATH, ZKEY_PATH),
    /Assert Failed|Error in template/i
  );
});

test("篡改公开输入: 修改 root / activityId / nullifier 均验证失败", async () => {
  const { proof, publicSignals } = await proveFor(MEMBERS, 1, ACTIVITY);

  for (const [i, name] of [
    [0, "root"],
    [1, "activityId"],
    [2, "nullifier"],
  ] as const) {
    const tampered = [...publicSignals];
    tampered[i] = "123456789";
    assert.equal(
      await verifyProof(vkey, tampered, proof),
      false,
      `篡改 ${name} 后验证必须失败`
    );
  }
});

test("篡改证明本体: 改动 pi_a 后验证失败", async () => {
  const { proof, publicSignals } = await proveFor(MEMBERS, 1, ACTIVITY);
  const badProof = JSON.parse(JSON.stringify(proof));
  badProof.pi_a[0] = (
    (BigInt(badProof.pi_a[0]) + 1n) %
    BigInt("21888242871839275222246405745257275088548364400416034343698204186575808495617")
  ).toString();
  assert.equal(await verifyProof(vkey, publicSignals, badProof), false);
});

test("nullifier 由秘密与活动域生成: 与 Poseidon(secret, activityId) 一致", async () => {
  const member = MEMBERS[1];
  const expected = await computeNullifier(member.secret, ACTIVITY);
  const direct = await poseidonHash([member.secret, ACTIVITY]);
  assert.equal(expected, direct);

  const { publicSignals } = await proveFor(MEMBERS, 1, ACTIVITY);
  assert.equal(publicSignals[2], expected, "公开 nullifier 必须等于 Poseidon(secret, activityId)");
});

test("换活动可使用: 同一秘密在不同活动产生不同 nullifier 且均可验证", async () => {
  const a = await proveFor(MEMBERS, 1, "10001");
  const b = await proveFor(MEMBERS, 1, "10002");
  assert.notEqual(a.publicSignals[2], b.publicSignals[2], "不同活动 nullifier 必须不同");
  assert.equal(await verifyProof(vkey, a.publicSignals, a.proof), true);
  assert.equal(await verifyProof(vkey, b.publicSignals, b.proof), true);
});

test("同活动不可重复: 重复 nullifier 被应用层拒绝", async () => {
  const registry = new NullifierRegistry(); // 内存注册表
  const verifier = new Verifier(vkey, registry);
  const { proof, publicSignals } = await proveFor(MEMBERS, 1, ACTIVITY);

  const first = await verifier.accept(proof, publicSignals, root, ACTIVITY);
  assert.equal(first.ok, true, `首次提交应通过: ${first.reason}`);

  // 同一证明再次提交 (重放)
  const replay = await verifier.accept(proof, publicSignals, root, ACTIVITY);
  assert.equal(replay.ok, false);
  assert.match(replay.reason, /nullifier already used/);

  // 同一成员重新生成的新证明, nullifier 相同, 同样拒绝
  const again = await proveFor(MEMBERS, 1, ACTIVITY);
  const dup = await verifier.accept(again.proof, again.publicSignals, root, ACTIVITY);
  assert.equal(dup.ok, false);
  assert.match(dup.reason, /nullifier already used/);

  // 换活动后 nullifier 改变, 可以接受
  const other = await proveFor(MEMBERS, 1, "10002");
  const okOther = await verifier.accept(other.proof, other.publicSignals, root, "10002");
  assert.equal(okOther.ok, true, `换活动应通过: ${okOther.reason}`);
});

test("应用层绑定: 证明的根/活动与预期不符时被拒绝", async () => {
  const verifier = new Verifier(vkey, new NullifierRegistry());
  const { proof, publicSignals } = await proveFor(MEMBERS, 1, ACTIVITY);

  const wrongRoot = await verifier.accept(proof, publicSignals, "999", ACTIVITY);
  assert.equal(wrongRoot.ok, false);
  assert.match(wrongRoot.reason, /root mismatch/);

  const wrongActivity = await verifier.accept(proof, publicSignals, root, "10003");
  assert.equal(wrongActivity.ok, false);
  assert.match(wrongActivity.reason, /activityId mismatch/);
});
