pragma circom 2.1.6;

include "circomlib/circuits/poseidon.circom";
include "circomlib/circuits/comparators.circom";
include "circomlib/circuits/bitify.circom";

/*
 * AnonymousEligibility
 *
 * Private inputs:
 *   secret        — user-held random secret
 *   age           — integer age bound into the leaf
 *   pathElements  — depth siblings of the Merkle inclusion proof
 *   pathIndices   — 0/1 selectors: 0 => current node is LEFT child, 1 => RIGHT child
 *
 * Public inputs:
 *   root          — Merkle root the leaf must belong to
 *   eventId       — event/domain identifier (per-activity nullifier domain)
 *   nullifier     — Poseidon(secret, eventId), revealed publicly
 *
 * Proven statements:
 *   1. leaf      = Poseidon(secret, age)
 *   2. nullifier = Poseidon(secret, eventId)
 *   3. 18 <= age <= 120
 *   4. leaf is a member of the depth-8 Merkle tree with the given root
 *
 * The same secret yields a different nullifier per eventId (reusable across
 * activities); twice in one activity reveals the same nullifier (replay block).
 */

// 2-input multiplexer: out = s ? b : a, without relying on circomlib's Mux.
template Switch() {
    signal input a;
    signal input b;
    signal input s;   // binary selector, constrained by the caller (pathIndices[*])
    signal output out;

    s * (1 - s) === 0;
    out <== a + s * (b - a);
}

template MerkleProof(depth) {
    signal input leaf;
    signal input pathElements[depth];
    signal input pathIndices[depth];
    signal output root;

    component selectLeft[depth];
    component selectRight[depth];
    component hash[depth];

    signal levelHash[depth + 1];
    levelHash[0] <== leaf;

    for (var i = 0; i < depth; i++) {
        // Poseidon is not ordered-input-independent: pick sides explicitly.
        selectLeft[i]  = Switch();
        selectRight[i] = Switch();
        selectLeft[i].a  <== levelHash[i];
        selectLeft[i].b  <== pathElements[i];
        selectLeft[i].s  <== pathIndices[i];

        selectRight[i].a <== pathElements[i];
        selectRight[i].b <== levelHash[i];
        selectRight[i].s <== pathIndices[i];

        hash[i] = Poseidon(2);
        hash[i].inputs[0] <== selectLeft[i].out;
        hash[i].inputs[1] <== selectRight[i].out;

        levelHash[i + 1] <== hash[i].out;
    }

    root <== levelHash[depth];
}

template AnonymousEligibility(depth) {
    // private
    signal input secret;
    signal input age;
    signal input pathElements[depth];
    signal input pathIndices[depth];

    // public
    signal input root;
    signal input eventId;
    signal input nullifier;

    // ---- age range: 18 <= age <= 120 ----
    // Bound age to 7 bits (0..127) first so the unsigned LessEqThan comparators
    // cannot be satisfied by large-field / negative encodings of age.
    component ageBits = Num2Bits(7);
    ageBits.in <== age;

    component ageAtLeast18 = LessEqThan(8);
    ageAtLeast18.in[0] <== 18;
    ageAtLeast18.in[1] <== age;
    ageAtLeast18.out === 1;

    component ageAtMost120 = LessEqThan(8);
    ageAtMost120.in[0] <== age;
    ageAtMost120.in[1] <== 120;
    ageAtMost120.out === 1;

    // ---- commitment leaf = Poseidon(secret, age) ----
    component leafHasher = Poseidon(2);
    leafHasher.inputs[0] <== secret;
    leafHasher.inputs[1] <== age;

    // ---- Merkle membership ----
    component tree = MerkleProof(depth);
    tree.leaf <== leafHasher.out;
    for (var i = 0; i < depth; i++) {
        tree.pathElements[i] <== pathElements[i];
        tree.pathIndices[i] <== pathIndices[i];
        // selectors must be bits (Switch also constrains this, repeated here
        // at the top level so malformed proofs fail unambiguously)
        pathIndices[i] * (1 - pathIndices[i]) === 0;
    }
    tree.root === root;

    // ---- domain-separated nullifier = Poseidon(secret, eventId) ----
    component nullifierHasher = Poseidon(2);
    nullifierHasher.inputs[0] <== secret;
    nullifierHasher.inputs[1] <== eventId;
    nullifierHasher.out === nullifier;
}

component main {public [root, eventId, nullifier]} = AnonymousEligibility(8);
