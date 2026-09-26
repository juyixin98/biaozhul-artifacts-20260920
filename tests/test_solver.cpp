// Unit tests for the SAT solver backend. Plain asserts, no framework.
#include <cstdlib>
#include <iostream>
#include <random>
#include <string>

#include "solver.hpp"

namespace {

int failures = 0;

#define CHECK(cond)                                                     \
    do {                                                                \
        if (!(cond)) {                                                  \
            std::cerr << "FAIL " << __FILE__ << ":" << __LINE__ << "  " \
                      << #cond << "\n";                                 \
            ++failures;                                                 \
        }                                                               \
    } while (0)

sat::CNF makeCnf(int n, std::vector<std::vector<int>> clauses) {
    sat::CNF cnf;
    cnf.numVars = n;
    cnf.clauses = std::move(clauses);
    return cnf;
}

void testNormalization() {
    // Repeated literals collapse.
    bool taut = false;
    std::vector<int> c = sat::normalizeClause({3, 1, 3, -2, 1}, taut);
    CHECK(!taut);
    CHECK((c == std::vector<int>{-2, 1, 3}));

    // Tautology x | -x detected.
    c = sat::normalizeClause({1, -1, 5}, taut);
    CHECK(taut);
    CHECK(c.empty());

    // Empty clause stays empty and is not a tautology.
    c = sat::normalizeClause({}, taut);
    CHECK(!taut);
    CHECK(c.empty());

    // normalizeCnf drops tautologies and records their indices.
    sat::CNF cnf = makeCnf(3, {{1, 2}, {2, -2}, {3}, {-1, 1}});
    sat::normalizeCnf(cnf);
    CHECK(cnf.clauses.size() == 2);
    CHECK(cnf.tautologyClauseIndices.size() == 2);
    CHECK(cnf.tautologyClauseIndices[0] == 1);
    CHECK(cnf.tautologyClauseIndices[1] == 3);
}

void testDimacs() {
    sat::ParseResult pr = sat::parseDimacs(
        "c comment\np cnf 3 2\n1 -2 0\n2 3 0\n");
    CHECK(pr.ok);
    CHECK(pr.cnf.numVars == 3);
    CHECK(pr.cnf.clauses.size() == 2);

    // Clause spanning two lines.
    pr = sat::parseDimacs("p cnf 2 1\n1\n2 0\n");
    CHECK(pr.ok);
    CHECK(pr.cnf.clauses.size() == 1);
    CHECK(pr.cnf.clauses[0].size() == 2);

    // Empty clause (bare 0).
    pr = sat::parseDimacs("p cnf 2 1\n0\n");
    CHECK(pr.ok);
    CHECK(pr.cnf.clauses.size() == 1);
    CHECK(pr.cnf.clauses[0].empty());

    // Wrong clause count is an error.
    pr = sat::parseDimacs("p cnf 2 2\n1 0\n");
    CHECK(!pr.ok);

    // Literal beyond declared vars is an error.
    pr = sat::parseDimacs("p cnf 2 1\n3 0\n");
    CHECK(!pr.ok);

    // Round trip.
    pr = sat::parseDimacs("p cnf 2 2\n1 -2 0\n-1 0\n");
    CHECK(pr.ok);
    sat::ParseResult pr2 = sat::parseDimacs(sat::toDimacs(pr.cnf));
    CHECK(pr2.ok);
    CHECK(pr2.cnf.clauses == pr.cnf.clauses);
}

void testJsonParse() {
    minijson::Value v;
    std::string err;
    CHECK(minijson::parse("{\"a\": [1, -2, true, null, \"x\"], \"b\": 3.5}", v, err));
    CHECK(v.find("a") != nullptr);
    CHECK(v.find("a")->arr.size() == 5);
    CHECK(v.find("a")->arr[1].i == -2);
    CHECK(v.find("b")->type == minijson::Value::NUM);

    // Round trip through dump.
    std::string dumped = minijson::dump(v);
    minijson::Value v2;
    CHECK(minijson::parse(dumped, v2, err));

    // Escapes.
    CHECK(minijson::parse("\"a\\n\\t\\u0041b\"", v, err));
    CHECK(v.s == "a\n\tAb");

    // Errors.
    CHECK(!minijson::parse("{bad", v, err));
    CHECK(!minijson::parse("[1,]", v, err));
    CHECK(!minijson::parse("1 2", v, err));
}

void testEdgeCases() {
    // Empty clause => immediate UNSAT with a single conflict node.
    sat::CNF cnf = makeCnf(2, {{}});
    sat::SolveResult r = sat::dpll(cnf);
    CHECK(r.status == sat::SolveResult::UNSAT);
    CHECK(r.proof.size() == 1);
    CHECK(r.proof[0].kind == sat::NODE_CONFLICT);
    std::string err;
    CHECK(sat::verifyProof(cnf, r, err));

    // No clauses at all, zero variables: one SAT node with an empty model.
    cnf = makeCnf(0, {});
    r = sat::dpll(cnf);
    CHECK(r.status == sat::SolveResult::SAT);
    CHECK(r.proof.size() == 1);
    CHECK(r.proof[0].kind == sat::NODE_SAT);
    CHECK(sat::verifyProof(cnf, r, err));

    // No clauses with declared variables: deterministic branch tree, still SAT.
    cnf = makeCnf(3, {});
    r = sat::dpll(cnf);
    CHECK(r.status == sat::SolveResult::SAT);
    CHECK(sat::verifyProof(cnf, r, err));

    // Unit clauses chain: (1) (-1 2) (-2 3) forces 1,2,3.
    cnf = makeCnf(3, {{1}, {-1, 2}, {-2, 3}});
    r = sat::dpll(cnf);
    CHECK(r.status == sat::SolveResult::SAT);
    CHECK(r.model[1] == 1 && r.model[2] == 1 && r.model[3] == 1);
    CHECK(r.decisions == 0);
    CHECK(sat::verifyProof(cnf, r, err));

    // Contradictory units: (1) (-1).
    cnf = makeCnf(1, {{1}, {-1}});
    r = sat::dpll(cnf);
    CHECK(r.status == sat::SolveResult::UNSAT);
    CHECK(sat::verifyProof(cnf, r, err));

    // Classic pigeonhole PHP(2,1): (a b) (-a) (-b) is UNSAT.
    cnf = makeCnf(2, {{1, 2}, {-1}, {-2}});
    r = sat::dpll(cnf);
    CHECK(r.status == sat::SolveResult::UNSAT);
    CHECK(sat::verifyProof(cnf, r, err));
}

void testDeterminism() {
    sat::CNF cnf = makeCnf(4, {{1, 2}, {-1, 3}, {-3, 4}, {-2, -4}, {2, 3}});
    sat::SolveResult a = sat::dpll(cnf);
    sat::SolveResult b = sat::dpll(cnf);
    CHECK(a.status == b.status);
    CHECK(a.proof.size() == b.proof.size());
    CHECK(a.decisions == b.decisions);
    CHECK(a.propagations == b.propagations);
    if (a.status == sat::SolveResult::SAT) CHECK(a.model == b.model);
}

void testRandomCrossCheck() {
    std::mt19937 rng(20260925);
    for (int trial = 0; trial < 400; ++trial) {
        int n = 1 + rng() % 6;                 // 1..6 vars
        int m = 1 + rng() % 12;                // 1..12 clauses
        std::vector<std::vector<int>> clauses;
        for (int i = 0; i < m; ++i) {
            int len = 1 + rng() % 3;           // 1..3 literals
            std::vector<int> clause;
            for (int j = 0; j < len; ++j) {
                int var = 1 + static_cast<int>(rng() % n);
                clause.push_back((rng() & 1) ? var : -var);
            }
            clauses.push_back(std::move(clause));
        }
        sat::CNF cnf = makeCnf(n, clauses);
        sat::normalizeCnf(cnf);

        sat::SolveResult r = sat::dpll(cnf);
        sat::BruteResult br;
        std::string err;
        CHECK(sat::bruteForce(cnf, br, err));
        bool dpllSat = (r.status == sat::SolveResult::SAT);
        CHECK(dpllSat == br.sat);
        CHECK(sat::verifyProof(cnf, r, err));
        if (dpllSat) {
            int falsified = -1;
            CHECK(sat::modelSatisfies(cnf, r.model, falsified));
        }
    }
}

void testTamperedProofRejected() {
    // UNSAT instance requiring both polarities of a branch:
    // the four clauses (a b)(-a b)(a -b)(-a -b) cannot all hold.
    sat::CNF cnf = makeCnf(2, {{1, 2}, {-1, 2}, {1, -2}, {-1, -2}});
    sat::SolveResult r = sat::dpll(cnf);
    CHECK(r.status == sat::SolveResult::UNSAT);
    std::string err;
    CHECK(sat::verifyProof(cnf, r, err));

    // Point every conflict leaf at clause 0; under each leaf's assignment
    // clause 0 is satisfied, so the conflict claim is bogus.
    sat::SolveResult bad = r;
    for (auto& n : bad.proof)
        if (n.kind == sat::NODE_CONFLICT) n.conflictClause = 0;
    CHECK(!sat::verifyProof(cnf, bad, err));

    // Claim SAT for an UNSAT instance.
    bad = r;
    bad.status = sat::SolveResult::SAT;
    bad.model = {0, 1, 1};
    CHECK(!sat::verifyProof(cnf, bad, err));

    // Drop the negative child of the branch: UNSAT is no longer established.
    bad = r;
    for (auto& n : bad.proof)
        if (n.kind == sat::NODE_BRANCH && n.negativeChild != -1) {
            n.negativeChild = -1;
            break;
        }
    CHECK(!sat::verifyProof(cnf, bad, err));
}

void testLimits() {
    // Node limit of 1 on a formula that needs branching => LIMIT.
    sat::CNF cnf = makeCnf(3, {{1, 2}, {-1, 3}, {-2, -3}});
    sat::SolveOptions opts;
    opts.nodeLimit = 1;
    sat::SolveResult r = sat::dpll(cnf, opts);
    CHECK(r.status == sat::SolveResult::LIMIT);
    std::string err;
    CHECK(!sat::verifyProof(cnf, r, err));

    // Brute force refuses > MAX_BRUTE_VARS.
    cnf = makeCnf(sat::MAX_BRUTE_VARS + 1, {{1}});
    sat::BruteResult br;
    CHECK(!sat::bruteForce(cnf, br, err));
}

}  // namespace

int main() {
    testNormalization();
    testDimacs();
    testJsonParse();
    testEdgeCases();
    testDeterminism();
    testRandomCrossCheck();
    testTamperedProofRejected();
    testLimits();
    if (failures == 0) {
        std::cout << "ALL TESTS PASSED\n";
        return 0;
    }
    std::cerr << failures << " check(s) failed\n";
    return 1;
}
