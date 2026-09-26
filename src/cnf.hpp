// CNF representation, parsing and normalization.
//
// Literal encoding: integer x with |x| in [1, num_vars]; x > 0 means x is true,
// x < 0 means |x| is false. A clause is a list of literals; an empty clause is
// the always-false clause.
#ifndef CNF_HPP
#define CNF_HPP

#include <string>
#include <vector>

#include "json.hpp"

namespace sat {

struct CNF {
    int numVars = 0;
    std::vector<std::vector<int>> clauses;
    // Indices (in `clauses`) of clauses that were removed as tautologies during
    // normalization. Kept for evidence/reporting; `clauses` itself has them gone.
    std::vector<int> tautologyClauseIndices;
};

struct ParseResult {
    bool ok = false;
    std::string error;
    CNF cnf;
};

// Accepts {"num_vars": n, "clauses": [[...], ...]} or just {"clauses": [...]}.
// num_vars defaults to the largest variable that appears.
ParseResult parseCnfFromJson(const minijson::Value& v);

// Parses standard DIMACS CNF text ("c" comments, "p cnf n m", 0-terminated clauses).
ParseResult parseDimacs(const std::string& text);

// Removes repeated literals; detects clauses containing both x and -x (tautology).
// Sets tautology=true for tautological clauses (the returned normalized clause is
// then empty and must be discarded by the caller).
std::vector<int> normalizeClause(const std::vector<int>& clause, bool& tautology);

// Normalizes every clause in place, dropping tautologies.
void normalizeCnf(CNF& cnf);

// Serializes the CNF back to DIMACS text.
std::string toDimacs(const CNF& cnf);

// Assignment values: 0 = unassigned, 1 = true, -1 = false; indexed 1..numVars.
int literalValue(int lit, const std::vector<int>& assignment);

// True iff every clause is satisfied by the (complete) assignment.
// Reports the index of the first falsified clause in falsifiedClause, or -1.
bool modelSatisfies(const CNF& cnf,
                    const std::vector<int>& assignment,
                    int& falsifiedClause);

}  // namespace sat

#endif
