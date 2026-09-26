#pragma once

#include <string>
#include <utility>
#include <vector>

#include "graph.hpp"
#include "treewidth.hpp"

struct VerificationResult {
    bool ok = false;
    int width = -1;
    size_t bagCount = 0;
    long edgesCovered = 0;
    long edgesTotal = 0;
    bool treeConnected = false;
    std::vector<std::string> errors;
    std::vector<std::string> warnings;
};

// Independently verify a tree decomposition against the original graph:
//   (T1) every bag is a subset of V and nonempty
//   (T2) every graph edge is contained in some bag         (edge cover)
//   (T3) bags containing any vertex form a connected subtree (running inter.)
//   (T4) the bag-adjacency structure is a single tree
// width reported is max|bag|-1. Join edges are tree edges too.
VerificationResult verifyDecomposition(const Graph& g,
                                       const TreeDecomposition& td);

// Independently replay an elimination order and check the reported steps
// (permutation validity, degrees, fill edges, width, bag contents).
bool verifyElimination(const Graph& g, const EliminationResult& r,
                       std::vector<std::string>* errors = nullptr);

// Check that the decomposition bag multiset is exactly the elimination bags
// {v} union its later (fill-aware) neighbors.
bool verifyBagsMatchOrder(const Graph& g, const std::vector<int>& order,
                          const TreeDecomposition& td,
                          std::vector<std::string>* errors = nullptr);
