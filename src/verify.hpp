#pragma once

#include "graph.hpp"

#include <string>
#include <vector>

// Independent verifier for AnalysisResult. Every check here is implemented
// from scratch (BFS / Kahn) rather than reusing the solver's data structures,
// so a solver bug can actually be caught.
namespace verify {

struct CheckReport {
    bool ok = true;
    std::vector<std::string> failures;

    void fail(const std::string& msg) {
        ok = false;
        failures.push_back(msg);
    }
};

CheckReport checkResult(const Graph& g, const AnalysisResult& result);

} // namespace verify
