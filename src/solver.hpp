// DPLL SAT solver with unit propagation, deterministic branching and a
// replayable search-tree proof, plus a naive exhaustive reference solver.
//
// No external SAT solver is used: this is a from-scratch textbook implementation
// intentionally limited to small instances (see MAX_* constants).
#ifndef SOLVER_HPP
#define SOLVER_HPP

#include <cstdint>
#include <string>
#include <vector>

#include "cnf.hpp"
#include "json.hpp"

namespace sat {

// Hard, deliberately small scale limits. The service rejects anything larger.
constexpr int MAX_DPLL_VARS = 100;
constexpr int MAX_BRUTE_VARS = 25;
constexpr int MAX_CLAUSES = 5000;
constexpr std::uint64_t DEFAULT_NODE_LIMIT = 200000;

struct PropStep {
    int literal = 0;       // literal made true by propagation
    int reasonClause = -1; // index into normalized clauses that forced it
};

enum NodeKind { NODE_BRANCH, NODE_CONFLICT, NODE_SAT };

struct ProofNode {
    int id = -1;
    NodeKind kind = NODE_BRANCH;
    // Propagations performed at this node, in order, after entering it.
    std::vector<PropStep> propagations;
    int decisionVar = 0;        // BRANCH only
    int positiveChild = -1;     // BRANCH: subtree for decisionVar = true
    int negativeChild = -1;     // BRANCH: subtree for decisionVar = false
    int conflictClause = -1;    // CONFLICT: falsified clause index
    std::vector<int> satModel;  // SAT: full assignment vector (index 1..n)
};

struct SolveResult {
    enum Status { SAT, UNSAT, LIMIT } status = LIMIT;
    std::vector<int> model;                 // SAT only; index 1..numVars
    std::vector<ProofNode> proof;           // full (or SAT-prefix) search tree
    int rootNode = -1;
    std::uint64_t decisions = 0;
    std::uint64_t propagations = 0;
    std::uint64_t conflicts = 0;
    std::uint64_t nodes = 0;
    std::uint64_t nodeLimit = DEFAULT_NODE_LIMIT;
    bool limitHit = false;
};

struct SolveOptions {
    std::uint64_t nodeLimit = DEFAULT_NODE_LIMIT;
    // When true, branch on the smallest unassigned variable trying positive
    // polarity first. This is the only branching rule; it is deterministic.
    bool deterministic = true;
};

// Runs DPLL. The CNF must already be normalized (tautologies removed, duplicate
// literals collapsed) but may contain an empty clause.
SolveResult dpll(const CNF& cnf, const SolveOptions& opts = SolveOptions{});

struct BruteResult {
    bool sat = false;
    std::vector<int> model;  // index 1..numVars
    std::uint64_t tested = 0;
};

// Naive reference: enumerate all 2^n assignments in binary order. Refuses
// formulas above MAX_BRUTE_VARS.
bool bruteForce(const CNF& cnf, BruteResult& out, std::string& error);

// Serializes a solve result (with proof) to JSON.
minijson::Value resultToJson(const SolveResult& r, const CNF& cnf);

// Rebuilds a SolveResult from a JSON document produced by resultToJson.
bool jsonToResult(const minijson::Value& v, SolveResult& r, std::string& err);

// Independent proof checker: replays the search tree from scratch using only the
// normalized CNF and the recorded decisions/propagations. Returns true iff the
// proof establishes exactly the claimed result. On failure, `error` explains why.
bool verifyProof(const CNF& cnf,
                 const SolveResult& r,
                 std::string& error);

}  // namespace sat

#endif
