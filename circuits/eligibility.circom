pragma circom 2.0.0;

include "poseidon.circom";
include "bitify.circom";

// 匿名资格电路 (Anonymous Eligibility Circuit)
//
// 私密输入:
//   age          持有人年龄
//   secret       持有人长期秘密 (身份盐, 不公开)
//   pathElements Merkle 路径上的兄弟节点
//   pathIndices  Merkle 路径方向 (0 = 当前节点在左, 1 = 在右)
//
// 公开输入:
//   root        深度 8 的 Merkle 树根 (成员名单承诺)
//   activityId  活动域标识 (不同活动取不同值)
//   nullifier   作废符 = Poseidon(secret, activityId)
//
// 证明陈述:
//   1. 18 <= age <= 120
//   2. 叶 = Poseidon(age, secret) 属于以 root 为根、深度为 levels 的 Merkle 树
//   3. nullifier 由同一 secret 与公开 activityId 正确生成
//      => 同一 secret 在同一活动下只能产生唯一 nullifier (防重复),
//         换活动 (换 activityId) 可再次使用, 且各活动之间不可链接。

// 计算 Poseidon Merkle 根并输出
template MerkleRoot(levels) {
    signal input leaf;
    signal input pathElements[levels];
    signal input pathIndices[levels];
    signal output root;

    component hashers[levels];
    signal nodes[levels + 1];
    nodes[0] <== leaf;

    for (var i = 0; i < levels; i++) {
        // 路径方向必须为比特
        pathIndices[i] * (1 - pathIndices[i]) === 0;

        hashers[i] = Poseidon(2);
        // pathIndices[i] == 0: (nodes[i], pathElements[i])
        // pathIndices[i] == 1: (pathElements[i], nodes[i])
        hashers[i].inputs[0] <== nodes[i] + pathIndices[i] * (pathElements[i] - nodes[i]);
        hashers[i].inputs[1] <== pathElements[i] + pathIndices[i] * (nodes[i] - pathElements[i]);
        nodes[i + 1] <== hashers[i].out;
    }

    root <== nodes[levels];
}

template Eligibility(levels) {
    // 私密输入
    signal input age;
    signal input secret;
    signal input pathElements[levels];
    signal input pathIndices[levels];

    // 公开输入
    signal input root;
    signal input activityId;
    signal input nullifier;

    // ---- 1. 年龄范围: 18 <= age <= 120 ----
    // age - 18 落在 [0, 102], 用 7 比特 (0..127) 约束即隐含下界;
    // 120 - age 同理隐含上界。两者同时成立 <=> 18 <= age <= 120。
    component lo = Num2Bits(7);
    lo.in <== age - 18;
    component hi = Num2Bits(7);
    hi.in <== 120 - age;

    // ---- 2. Merkle 成员资格: 叶 = Poseidon(age, secret) ----
    component leafHash = Poseidon(2);
    leafHash.inputs[0] <== age;
    leafHash.inputs[1] <== secret;

    component merkle = MerkleRoot(levels);
    merkle.leaf <== leafHash.out;
    for (var i = 0; i < levels; i++) {
        merkle.pathElements[i] <== pathElements[i];
        merkle.pathIndices[i] <== pathIndices[i];
    }
    root === merkle.root;

    // ---- 3. nullifier 绑定: Poseidon(secret, activityId) ----
    component nf = Poseidon(2);
    nf.inputs[0] <== secret;
    nf.inputs[1] <== activityId;
    nullifier === nf.out;
}

component main {public [root, activityId, nullifier]} = Eligibility(8);
