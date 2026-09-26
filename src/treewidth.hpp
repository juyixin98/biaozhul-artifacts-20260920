#pragma once

#include <cstdint>
#include <string>
#include <utility>
#include <vector>

#include "graph.hpp"

// Size limits (see README "Scale limits"). Core heuristics are exact
// algorithms over small data; no external solver is used anywhere.
constexpr int MAX_VERTICES = 100;          // heuristic input cap
constexpr int MAX_EXACT_N_DEFAULT = 10;    // memoized exact search default cap
constexpr int MAX_EXACT_N_HARD = 11;       // hard cap (55 edge bits fit uint64)
constexpr int MAX_NAIVE_N = 8;             // factorial reference cap

struct ElimStep {
    int position = 0;
    int vertex = 0;
    int degreeAtElimination = 0;
    // Vertex + its remaining neighbors form this elimination bag.
    std::vector<int> bag;
    // Fill edges (chords) added when this vertex was eliminated.
    std::vector<std::pair<int, int>> addedFill;
};

struct EliminationResult {
    std::string heuristic;
    std::vector<int> order;                // vertex indices, elimination order
    std::vector<ElimStep> steps;
    int width = 0;                          // max over (degree at elimination)
    long long totalFill = 0;
    double elapsedMs = 0.0;
};

struct TDBag {
    int id = 0;
    std::vector<int> vertices;
    int parent = -1;                        // -1 = tree root
};

struct TreeDecomposition {
    std::vector<TDBag> bags;
    std::vector<std::pair<int, int>> treeEdges;
    int width = 0;
    int roots = 0;
    // Edges added to join per-component elimination forests into one tree.
    std::vector<std::pair<int, int>> rootJoinEdges;
};

struct ExactResult {
    bool feasible = false;
    int width = -1;
    std::vector<int> order;
    long long memoStates = 0;
    long long searchNodes = 0;
    bool hitHardLimit = false;
    double elapsedMs = 0.0;
};

struct NaiveResult {
    int width = -1;
    std::vector<int> order;
    long long permutations = 0;
    double elapsedMs = 0.0;
};

// Minimum-fill heuristic: eliminate the vertex creating the fewest fill
// edges; ties broken by smallest current degree, then smallest index.
EliminationResult minFillOrder(const Graph& g);

// Minimum-degree heuristic (provided as a second baseline).
EliminationResult minDegreeOrder(const Graph& g);

// Build the tree decomposition from an elimination order (classic
// "earliest later neighbor" parent rule). For disconnected graphs the
// elimination forest is joined into a single tree via rootJoinEdges.
TreeDecomposition buildTreeDecomposition(const Graph& g,
                                         const std::vector<int>& order);

// Width obtained from a fixed elimination order (independently usable;
// the exact/naive searches use their own bit-mask implementations).
int eliminationWidth(const Graph& g, const std::vector<int>& order);

// Memoized exact search over elimination choices (branch-and-bound DFS).
// Computes the *optimal* treewidth for small graphs. n must be <= limit
// (<= MAX_EXACT_N_HARD); reports hitHardLimit=true when it refuses.
ExactResult exactOptimal(const Graph& g, int limit = MAX_EXACT_N_DEFAULT);

// Naive reference: enumerate all n! permutations, no pruning, no memo.
// Only for n <= MAX_NAIVE_N. Used to cross-check the memoized exact search.
NaiveResult naiveOptimal(const Graph& g);
