#include "cnf.hpp"

#include <algorithm>
#include <cctype>
#include <sstream>
#include <unordered_set>

namespace sat {

ParseResult parseCnfFromJson(const minijson::Value& v) {
    ParseResult r;
    if (v.type != minijson::Value::OBJ) {
        r.error = "request body must be a JSON object";
        return r;
    }
    const minijson::Value* clausesVal = v.find("clauses");
    if (clausesVal == nullptr) {
        r.error = "missing required field \"clauses\"";
        return r;
    }
    if (clausesVal->type != minijson::Value::ARR) {
        r.error = "\"clauses\" must be an array of arrays of integers";
        return r;
    }

    CNF cnf;
    if (const minijson::Value* nv = v.find("num_vars")) {
        if (nv->type != minijson::Value::INT || nv->i <= 0) {
            r.error = "\"num_vars\" must be a positive integer";
            return r;
        }
        cnf.numVars = static_cast<int>(nv->i);
    }

    for (size_t ci = 0; ci < clausesVal->arr.size(); ++ci) {
        const minijson::Value& clauseVal = clausesVal->arr[ci];
        if (clauseVal.type != minijson::Value::ARR) {
            r.error = "clause #" + std::to_string(ci) + " must be an array";
            return r;
        }
        std::vector<int> clause;
        for (size_t li = 0; li < clauseVal.arr.size(); ++li) {
            const minijson::Value& litVal = clauseVal.arr[li];
            if (litVal.type != minijson::Value::INT) {
                r.error = "clause #" + std::to_string(ci) + " literal #" +
                          std::to_string(li) + " must be an integer";
                return r;
            }
            if (litVal.i == 0) {
                r.error = "clause #" + std::to_string(ci) +
                          " literal must be non-zero (0 is not a literal)";
                return r;
            }
            if (litVal.i < -1000000 || litVal.i > 1000000) {
                r.error = "clause #" + std::to_string(ci) +
                          " literal out of supported range";
                return r;
            }
            clause.push_back(static_cast<int>(litVal.i));
        }
        cnf.clauses.push_back(std::move(clause));
    }

    int maxVar = cnf.numVars;
    for (const auto& clause : cnf.clauses)
        for (int lit : clause)
            maxVar = std::max(maxVar, std::abs(lit));
    if (cnf.numVars == 0) cnf.numVars = maxVar;
    if (maxVar > cnf.numVars) {
        r.error = "literal references variable " + std::to_string(maxVar) +
                  " but num_vars is " + std::to_string(cnf.numVars);
        return r;
    }
    r.cnf = std::move(cnf);
    r.ok = true;
    return r;
}

ParseResult parseDimacs(const std::string& text) {
    ParseResult r;
    CNF cnf;
    std::vector<int> current;
    bool headerSeen = false;
    int declaredClauses = -1;
    int clauseCount = 0;
    size_t lineNo = 0;
    std::istringstream in(text);
    std::string line;

    auto finishClause = [&]() {
        // A terminating 0 always ends a clause, even an empty one (bare "0").
        cnf.clauses.push_back(current);
        current.clear();
        ++clauseCount;
    };

    while (std::getline(in, line)) {
        ++lineNo;
        // Trim leading whitespace to inspect the first token.
        size_t p = line.find_first_not_of(" \t\r");
        if (p == std::string::npos) continue;
        if (line[p] == 'c') continue;  // comment line
        if (line[p] == 'p') {
            std::istringstream hdr(line.substr(p + 1));
            std::string fmt;
            int n = 0, m = 0;
            if (!(hdr >> fmt >> n >> m) || fmt != "cnf") {
                r.error = "line " + std::to_string(lineNo) +
                          ": malformed problem line (expected \"p cnf <n> <m>\")";
                return r;
            }
            if (n < 0 || m < 0) {
                r.error = "line " + std::to_string(lineNo) +
                          ": negative n or m in problem line";
                return r;
            }
            cnf.numVars = n;
            declaredClauses = m;
            headerSeen = true;
            continue;
        }
        if (!headerSeen) {
            r.error = "line " + std::to_string(lineNo) +
                      ": clause data before \"p cnf\" header";
            return r;
        }
        std::istringstream nums(line.substr(p));
        int x = 0;
        while (nums >> x) {
            if (x == 0) {
                finishClause();
            } else {
                if (std::abs(x) > cnf.numVars) {
                    r.error = "line " + std::to_string(lineNo) +
                              ": literal " + std::to_string(x) +
                              " exceeds declared variable count " +
                              std::to_string(cnf.numVars);
                    return r;
                }
                current.push_back(x);
            }
        }
        // DIMACS allows clauses to span several lines; no 0 yet means continue.
    }
    // A trailing clause without a terminating 0 is accepted only if non-empty.
    if (!current.empty()) finishClause();
    if (declaredClauses >= 0 && declaredClauses != clauseCount) {
        r.error = "header declares " + std::to_string(declaredClauses) +
                  " clauses but found " + std::to_string(clauseCount);
        return r;
    }
    if (cnf.numVars == 0) {
        int maxVar = 0;
        for (const auto& clause : cnf.clauses)
            for (int lit : clause) maxVar = std::max(maxVar, std::abs(lit));
        cnf.numVars = maxVar;
    }
    r.cnf = std::move(cnf);
    r.ok = true;
    return r;
}

std::vector<int> normalizeClause(const std::vector<int>& clause, bool& tautology) {
    tautology = false;
    std::unordered_set<int> seen;
    for (int lit : clause) seen.insert(lit);
    std::vector<int> out(seen.begin(), seen.end());
    std::sort(out.begin(), out.end());
    for (int lit : out) {
        if (seen.count(-lit)) {
            tautology = true;
            return {};
        }
    }
    return out;
}

void normalizeCnf(CNF& cnf) {
    std::vector<std::vector<int>> normalized;
    normalized.reserve(cnf.clauses.size());
    cnf.tautologyClauseIndices.clear();
    for (size_t i = 0; i < cnf.clauses.size(); ++i) {
        bool taut = false;
        std::vector<int> c = normalizeClause(cnf.clauses[i], taut);
        if (taut) {
            cnf.tautologyClauseIndices.push_back(static_cast<int>(i));
        } else {
            normalized.push_back(std::move(c));
        }
    }
    cnf.clauses = std::move(normalized);
}

std::string toDimacs(const CNF& cnf) {
    std::string out = "p cnf " + std::to_string(cnf.numVars) + " " +
                      std::to_string(cnf.clauses.size()) + "\n";
    for (const auto& clause : cnf.clauses) {
        for (int lit : clause) {
            out += std::to_string(lit);
            out += ' ';
        }
        out += "0\n";
    }
    return out;
}

int literalValue(int lit, const std::vector<int>& assignment) {
    int var = std::abs(lit);
    if (var >= static_cast<int>(assignment.size()) || assignment[var] == 0) return 0;
    return lit > 0 ? assignment[var] : -assignment[var];
}

bool modelSatisfies(const CNF& cnf,
                    const std::vector<int>& assignment,
                    int& falsifiedClause) {
    falsifiedClause = -1;
    if (static_cast<int>(assignment.size()) < cnf.numVars + 1) {
        return false;
    }
    for (size_t i = 0; i < cnf.clauses.size(); ++i) {
        bool satisfied = false;
        for (int lit : cnf.clauses[i]) {
            if (literalValue(lit, assignment) == 1) {
                satisfied = true;
                break;
            }
        }
        if (!satisfied) {
            falsifiedClause = static_cast<int>(i);
            return false;
        }
    }
    return true;
}

}  // namespace sat
