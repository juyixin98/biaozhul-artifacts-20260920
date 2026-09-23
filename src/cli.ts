import * as fs from "fs";
import * as path from "path";
import { buildCircuitInput, buildTree, Member } from "./eligibility";
import { generateProof } from "./prover";
import { NullifierRegistry, Verifier, verifyProof } from "./verifier";

const BUILD_DIR = path.join(__dirname, "..", "build");
const WASM_PATH = path.join(BUILD_DIR, "eligibility_js", "eligibility.wasm");
const ZKEY_PATH = path.join(BUILD_DIR, "eligibility_final.zkey");
const VKEY_PATH = path.join(BUILD_DIR, "verification_key.json");
const REGISTRY_PATH = path.join(BUILD_DIR, "nullifiers.json");

interface ProveInputFile {
  activityId: string;
  proverIndex: number;
  members: Member[];
}

function requireArtifacts(): void {
  for (const p of [WASM_PATH, ZKEY_PATH, VKEY_PATH]) {
    if (!fs.existsSync(p)) {
      console.error(`缺少构建产物: ${p}\n请先运行: npm run setup`);
      process.exit(1);
    }
  }
}

async function cmdProve(inputFile: string): Promise<void> {
  requireArtifacts();
  const spec: ProveInputFile = JSON.parse(fs.readFileSync(inputFile, "utf8"));
  const member = spec.members[spec.proverIndex];
  if (!member) {
    throw new Error(`proverIndex ${spec.proverIndex} 超出成员数量`);
  }

  const tree = await buildTree(spec.members);
  const circuitInput = await buildCircuitInput(
    spec.members,
    spec.proverIndex,
    spec.activityId
  );

  console.log(`成员数: ${spec.members.length}, 证明人索引: ${spec.proverIndex}`);
  console.log(`Merkle 根 (公开): ${tree.root}`);
  console.log(`活动 ID (公开):   ${spec.activityId}`);
  console.log(`nullifier (公开): ${circuitInput.nullifier}`);

  const t0 = Date.now();
  const { proof, publicSignals } = await generateProof(
    circuitInput as unknown as Record<string, unknown>,
    WASM_PATH,
    ZKEY_PATH
  );
  console.log(`证明生成耗时: ${Date.now() - t0} ms`);

  fs.writeFileSync(
    path.join(BUILD_DIR, "proof.json"),
    JSON.stringify(proof, null, 2)
  );
  fs.writeFileSync(
    path.join(BUILD_DIR, "public.json"),
    JSON.stringify(publicSignals, null, 2)
  );
  fs.writeFileSync(path.join(BUILD_DIR, "root.txt"), tree.root + "\n");
  fs.writeFileSync(
    path.join(BUILD_DIR, "activity_id.txt"),
    String(spec.activityId) + "\n"
  );
  console.log("已写出 build/proof.json 与 build/public.json");
}

async function cmdVerify(proofFile: string, publicFile: string): Promise<void> {
  requireArtifacts();
  const proof = JSON.parse(fs.readFileSync(proofFile, "utf8"));
  const publicSignals: string[] = JSON.parse(
    fs.readFileSync(publicFile, "utf8")
  );
  const vkey = JSON.parse(fs.readFileSync(VKEY_PATH, "utf8"));

  const cryptoOk = await verifyProof(vkey, publicSignals, proof);
  console.log(`密码学验证 (groth16.verify): ${cryptoOk ? "通过" : "失败"}`);
  if (!cryptoOk) process.exit(1);

  // 应用层校验: 绑定当前根/活动, 并登记 nullifier 防重放
  const expectedRoot = fs.existsSync(path.join(BUILD_DIR, "root.txt"))
    ? fs.readFileSync(path.join(BUILD_DIR, "root.txt"), "utf8").trim()
    : publicSignals[0];
  const expectedActivity = fs.existsSync(path.join(BUILD_DIR, "activity_id.txt"))
    ? fs.readFileSync(path.join(BUILD_DIR, "activity_id.txt"), "utf8").trim()
    : publicSignals[1];

  const verifier = new Verifier(vkey, new NullifierRegistry(REGISTRY_PATH));
  const result = await verifier.accept(
    proof,
    publicSignals,
    expectedRoot,
    expectedActivity
  );
  console.log(`应用层校验: ${result.reason}`);
  if (!result.ok) process.exit(1);
  console.log(`nullifier 已登记: ${result.nullifier}`);
}

async function cmdDemo(): Promise<void> {
  requireArtifacts();
  const vkey = JSON.parse(fs.readFileSync(VKEY_PATH, "utf8"));
  const members: Member[] = [
    { age: 34, secret: "192837465012345678901234567890" },
    { age: 22, secret: "564738291056473829105647382910" },
    { age: 51, secret: "102938475610293847561029384756" },
    { age: 19, secret: "887766554433221100998877665544" },
  ];
  const activityId = "2026092201";

  const tree = await buildTree(members);
  console.log(`[demo] 成员 ${members.length} 人, Merkle 根: ${tree.root}`);

  const input = await buildCircuitInput(members, 1, activityId);
  const { proof, publicSignals } = await generateProof(
    input as unknown as Record<string, unknown>,
    WASM_PATH,
    ZKEY_PATH
  );
  const ok = await verifyProof(vkey, publicSignals, proof);
  console.log(`[demo] 合法证明验证: ${ok ? "通过" : "失败"}`);

  // 篡改公开输入 (换根) 必须失败
  const tampered = [...publicSignals];
  tampered[0] = "123456789";
  const okTampered = await verifyProof(vkey, tampered, proof);
  console.log(`[demo] 篡改 root 后验证: ${okTampered ? "通过(异常!)" : "失败(符合预期)"}`);

  // 同一成员换活动 => 新 nullifier, 可再次通过
  const input2 = await buildCircuitInput(members, 1, "2026092202");
  const r2 = await generateProof(
    input2 as unknown as Record<string, unknown>,
    WASM_PATH,
    ZKEY_PATH
  );
  const ok2 = await verifyProof(vkey, r2.publicSignals, r2.proof);
  console.log(
    `[demo] 换活动: nullifier ${input.nullifier.slice(0, 16)}... -> ${input2.nullifier.slice(0, 16)}..., 验证 ${ok2 ? "通过" : "失败"}`
  );

  if (!ok || okTampered || !ok2) process.exit(1);
}

async function main(): Promise<void> {
  const [cmd, ...args] = process.argv.slice(2);
  switch (cmd) {
    case "prove":
      await cmdProve(args[0] ?? "inputs/example.json");
      break;
    case "verify":
      await cmdVerify(
        args[0] ?? path.join(BUILD_DIR, "proof.json"),
        args[1] ?? path.join(BUILD_DIR, "public.json")
      );
      break;
    case "demo":
      await cmdDemo();
      break;
    default:
      console.log("用法:");
      console.log("  npm run prove -- inputs/example.json   生成证明");
      console.log("  npm run verify                          验证证明并登记 nullifier");
      console.log("  npm run demo                            端到端演示");
      process.exit(cmd ? 1 : 0);
  }
}

main()
  .then(() => {
    // snarkjs 的 worker 线程在 Node 18 下偶发残留, 任务完成后显式退出
    process.exit(0);
  })
  .catch((err) => {
    console.error("执行失败:", err.message ?? err);
    process.exit(1);
  });
