#include "solver.hpp"

#include <algorithm>
#include <cmath>

namespace sat {

namespace {

// Mutable DPLL state. Every assignment made inside one recursive call is
// tracked in `here`, so UNSAT exits roll back exactly what they added.
class DpllEngine {
public:
    DpllEngine(const CNF& cnf, const SolveOptions& opts)
        : cnf_(cnf), opts_(opts), assignment_(cnf.numVars + 1, 0) {}

    SolveResult run() {
        SolveResult r;
        r.nodeLimit = opts_.nodeLimit;
        int root = solve();
        if (satNodeId_ != -1) {
            r.model = proof_[satNodeId_].satModel;
        }
        r.proof = std::move(proof_);
        r.rootNode = root;
        r.decisions = decisions_;
        r.propagations = propagations_;
        r.conflicts = conflicts_;
        r.nodes = nodeCount_;
        if (limitHit_) {
            r.status = SolveResult::LIMIT;
            r.limitHit = true;
        } else if (satNodeId_ != -1) {
            r.status = SolveResult::SAT;
        } else {
            r.status = SolveResult::UNSAT;
        }
        return r;
    }

private:
    const CNF& cnf_;
    SolveOptions opts_;
    std::vector<int> assignment_;          // 0 unassigned, 1 true, -1 false
    std::vector<ProofNode> proof_;
    bool limitHit_ = false;
    std::uint64_t nodeCount_ = 0;
    std::uint64_t decisions_ = 0;
    std::uint64_t propagations_ = 0;
    std::uint64_t conflicts_ = 0;
    int satNodeId_ = -1;

    void assignLit(int lit) {
        assignment_[std::abs(lit)] = lit > 0 ? 1 : -1;
    }
    void unassignVar(int var) { assignment_[var] = 0; }

    // Creates a proof node and returns its index.
    int addNode(ProofNode node) {
        node.id = static_cast<int>(proof_.size());
        proof_.push_back(std::move(node));
        return node.id;
    }

    // Recursive DPLL. On UNSAT return, the assignment is restored to the state
    // on entry (the caller owns any decision literal it passed via assignment_).
    int solve() {
        if (++nodeCount_ > opts_.nodeLimit) {
            limitHit_ = true;
            return -1;
        }

        std::vector<int> here;  // variables assigned at this node
        std::vector<PropStep> steps;

        // Unit propagation to fixpoint, with immediate conflict detection.
        while (true) {
            int unitClause = -1;
            int unitLiteral = 0;
            int falsifiedClause = -1;

            for (size_t ci = 0; ci < cnf_.clauses.size(); ++ci) {
                const std::vector<int>& clause = cnf_.clauses[ci];
                int unassigned = 0;
                int theLiteral = 0;
                bool satisfied = false;
                for (int lit : clause) {
                    int val = literalValue(lit, assignment_);
                    if (val == 1) { satisfied = true; break; }
                    if (val == 0) { ++unassigned; theLiteral = lit; }
                }
                if (satisfied) continue;
                if (unassigned == 0) { falsifiedClause = static_cast<int>(ci); break; }
                if (unassigned == 1 && unitClause == -1) {
                    unitClause = static_cast<int>(ci);
                    unitLiteral = theLiteral;
                }
            }

            if (falsifiedClause != -1) {
                ProofNode node;
                node.kind = NODE_CONFLICT;
                node.propagations = std::move(steps);
                node.conflictClause = falsifiedClause;
                int id = addNode(std::move(node));
                for (int var : here) unassignVar(var);
                ++conflicts_;
                return id;
            }
            if (unitClause == -1) break;  // fixpoint reached

            assignLit(unitLiteral);
            here.push_back(std::abs(unitLiteral));
            steps.push_back(PropStep{unitLiteral, unitClause});
            ++propagations_;
        }

        // All variables assigned without conflict => satisfying model.
        int firstUnassigned = -1;
        for (int v = 1; v <= cnf_.numVars; ++v) {
            if (assignment_[v] == 0) { firstUnassigned = v; break; }
        }
        if (firstUnassigned == -1) {
            ProofNode node;
            node.kind = NODE_SAT;
            node.propagations = std::move(steps);
            node.satModel = assignment_;  // snapshot (entry 0 unused)
            int id = addNode(std::move(node));
            satNodeId_ = id;
            // No rollback needed: search terminates on SAT.
            return id;
        }

        // Deterministic branching: smallest unassigned variable, positive first.
        ProofNode branch;
        branch.kind = NODE_BRANCH;
        branch.propagations = std::move(steps);
        branch.decisionVar = firstUnassigned;
        int branchId = addNode(std::move(branch));
        ++decisions_;

        assignLit(firstUnassigned);
        here.push_back(firstUnassigned);
        int posChild = solve();
        if (limitHit_) return -1;
        proof_[branchId].positiveChild = posChild;
        if (subtreeIsSat(posChild)) return branchId;  // satNodeId_ set by the leaf
        unassignVar(firstUnassigned);
        here.pop_back();

        assignLit(-firstUnassigned);
        here.push_back(firstUnassigned);
        int negChild = solve();
        if (limitHit_) return -1;
        unassignVar(firstUnassigned);
        here.pop_back();
        proof_[branchId].negativeChild = negChild;

        for (int var : here) unassignVar(var);
        return branchId;
    }

    bool subtreeIsSat(int id) const {
        const ProofNode& n = proof_[id];
        if (n.kind == NODE_SAT) return true;
        if (n.kind == NODE_CONFLICT) return false;
        if (n.positiveChild != -1 && subtreeIsSat(n.positiveChild)) return true;
        if (n.negativeChild != -1 && subtreeIsSat(n.negativeChild)) return true;
        return false;
    }
};

}  // namespace

SolveResult dpll(const CNF& cnf, const SolveOptions& opts) {
    DpllEngine engine(cnf, opts);
    return engine.run();
}

bool bruteForce(const CNF& cnf, BruteResult& out, std::string& error) {
    if (cnf.numVars > MAX_BRUTE_VARS) {
        error = "brute-force reference supports at most " +
                std::to_string(MAX_BRUTE_VARS) +
                " variables (got " + std::to_string(cnf.numVars) + ")";
        return false;
    }
    std::vector<int> assignment(cnf.numVars + 1, 0);
    std::uint64_t total = cnf.numVars == 0
                              ? 1ULL
                              : (1ULL << static_cast<unsigned>(cnf.numVars));
    for (std::uint64_t mask = 0; mask < total; ++mask) {
        ++out.tested;
        for (int v = 1; v <= cnf.numVars; ++v) {
            assignment[v] = (mask >> static_cast<unsigned>(v - 1)) & 1ULL ? 1 : -1;
        }
        int falsified = -1;
        if (modelSatisfies(cnf, assignment, falsified)) {
            out.sat = true;
            out.model = assignment;
            return true;
        }
    }
    out.sat = false;
    return true;
}

minijson::Value resultToJson(const SolveResult& r, const CNF& cnf) {
    using minijson::Value;
    Value root = Value::makeObj();
    const char* status =
        r.status == SolveResult::SAT ? "sat"
        : r.status == SolveResult::UNSAT ? "unsat" : "limit";
    root.set("status", Value::makeStr(status));
    root.set("num_vars", Value::makeInt(cnf.numVars));

    Value clauses = Value::makeArr();
    for (const auto& clause : cnf.clauses) {
        Value arr = Value::makeArr();
        for (int lit : clause) arr.arr.push_back(Value::makeInt(lit));
        clauses.arr.push_back(std::move(arr));
    }
    root.set("normalized_clauses", std::move(clauses));

    Value tauts = Value::makeArr();
    for (int idx : cnf.tautologyClauseIndices) tauts.arr.push_back(Value::makeInt(idx));
    root.set("tautology_clause_indices", std::move(tauts));

    if (r.status == SolveResult::SAT) {
        Value model = Value::makeArr();
        for (int v = 1; v <= cnf.numVars; ++v) {
            Value entry = Value::makeObj();
            entry.set("variable", Value::makeInt(v));
            entry.set("value", Value::makeBool(r.model[v] == 1));
            model.arr.push_back(std::move(entry));
        }
        root.set("model", std::move(model));
    } else {
        root.set("model", Value::makeNull());
    }

    Value stats = Value::makeObj();
    stats.set("nodes", Value::makeInt(static_cast<long long>(r.nodes)));
    stats.set("decisions", Value::makeInt(static_cast<long long>(r.decisions)));
    stats.set("propagations", Value::makeInt(static_cast<long long>(r.propagations)));
    stats.set("conflicts", Value::makeInt(static_cast<long long>(r.conflicts)));
    stats.set("node_limit", Value::makeInt(static_cast<long long>(r.nodeLimit)));
    root.set("stats", std::move(stats));

    Value proof = Value::makeObj();
    proof.set("root", Value::makeInt(r.rootNode));
    Value nodes = Value::makeArr();
    for (const ProofNode& n : r.proof) {
        Value jn = Value::makeObj();
        jn.set("id", Value::makeInt(n.id));
        const char* kind = n.kind == NODE_BRANCH ? "branch"
                           : n.kind == NODE_CONFLICT ? "conflict" : "sat";
        jn.set("kind", Value::makeStr(kind));
        jn.set("decision_var", n.decisionVar ? Value::makeInt(n.decisionVar)
                                             : Value::makeNull());
        Value props = Value::makeArr();
        for (const PropStep& p : n.propagations) {
            Value jp = Value::makeObj();
            jp.set("literal", Value::makeInt(p.literal));
            jp.set("reason_clause", Value::makeInt(p.reasonClause));
            props.arr.push_back(std::move(jp));
        }
        jn.set("propagations", std::move(props));
        jn.set("positive_child",
               n.positiveChild == -1 ? Value::makeNull() : Value::makeInt(n.positiveChild));
        jn.set("negative_child",
               n.negativeChild == -1 ? Value::makeNull() : Value::makeInt(n.negativeChild));
        jn.set("conflict_clause",
               n.conflictClause == -1 ? Value::makeNull()
                                      : Value::makeInt(n.conflictClause));
        if (n.kind == NODE_SAT) {
            Value asg = Value::makeArr();
            for (int v = 1; v < static_cast<int>(n.satModel.size()); ++v)
                asg.arr.push_back(Value::makeInt(n.satModel[v]));
            jn.set("assignment", std::move(asg));
        }
        nodes.arr.push_back(std::move(jn));
    }
    proof.set("nodes", std::move(nodes));
    root.set("proof", std::move(proof));
    return root;
}

bool jsonToResult(const minijson::Value& v, SolveResult& r, std::string& err) {
    using minijson::Value;
    const Value* status = v.find("status");
    if (status == nullptr || status->type != Value::STR) {
        err = "missing \"status\"";
        return false;
    }
    if (status->s == "sat") r.status = SolveResult::SAT;
    else if (status->s == "unsat") r.status = SolveResult::UNSAT;
    else if (status->s == "limit") r.status = SolveResult::LIMIT;
    else {
        err = "unknown status \"" + status->s + "\"";
        return false;
    }

    if (const Value* model = v.find("model")) {
        if (model->type == Value::ARR) {
            r.model.push_back(0);  // index 0 unused
            for (const Value& entry : model->arr) {
                const Value* val = entry.find("value");
                if (val == nullptr || val->type != Value::BOOL) {
                    err = "model entries must have boolean \"value\"";
                    return false;
                }
                r.model.push_back(val->b ? 1 : -1);
            }
        }
    }

    const Value* proof = v.find("proof");
    if (proof == nullptr || proof->type != Value::OBJ) {
        err = "missing \"proof\" object";
        return false;
    }
    const Value* root = proof->find("root");
    const Value* nodes = proof->find("nodes");
    if (root == nullptr || root->type != Value::INT ||
        nodes == nullptr || nodes->type != Value::ARR) {
        err = "proof must contain integer \"root\" and array \"nodes\"";
        return false;
    }
    r.rootNode = static_cast<int>(root->i);
    for (const Value& jn : nodes->arr) {
        ProofNode n;
        const Value* kind = jn.find("kind");
        if (kind == nullptr || kind->type != Value::STR) {
            err = "proof node missing \"kind\"";
            return false;
        }
        if (kind->s == "branch") n.kind = NODE_BRANCH;
        else if (kind->s == "conflict") n.kind = NODE_CONFLICT;
        else if (kind->s == "sat") n.kind = NODE_SAT;
        else {
            err = "unknown node kind \"" + kind->s + "\"";
            return false;
        }
        if (const Value* dv = jn.find("decision_var"))
            if (dv->type == Value::INT) n.decisionVar = static_cast<int>(dv->i);
        if (const Value* pc = jn.find("positive_child"))
            if (pc->type == Value::INT) n.positiveChild = static_cast<int>(pc->i);
        if (const Value* nc = jn.find("negative_child"))
            if (nc->type == Value::INT) n.negativeChild = static_cast<int>(nc->i);
        if (const Value* cc = jn.find("conflict_clause"))
            if (cc->type == Value::INT) n.conflictClause = static_cast<int>(cc->i);
        if (const Value* props = jn.find("propagations")) {
            if (props->type != Value::ARR) {
                err = "\"propagations\" must be an array";
                return false;
            }
            for (const Value& jp : props->arr) {
                const Value* lit = jp.find("literal");
                const Value* reason = jp.find("reason_clause");
                if (lit == nullptr || lit->type != Value::INT ||
                    reason == nullptr || reason->type != Value::INT) {
                    err = "propagation entries need integer \"literal\" and \"reason_clause\"";
                    return false;
                }
                n.propagations.push_back(
                    PropStep{static_cast<int>(lit->i), static_cast<int>(reason->i)});
            }
        }
        if (const Value* asg = jn.find("assignment")) {
            if (asg->type != Value::ARR) {
                err = "\"assignment\" must be an array";
                return false;
            }
            n.satModel.push_back(0);
            for (const Value& x : asg->arr) {
                if (x.type != Value::INT) {
                    err = "assignment entries must be integers";
                    return false;
                }
                n.satModel.push_back(static_cast<int>(x.i));
            }
        }
        n.id = static_cast<int>(r.proof.size());
        r.proof.push_back(std::move(n));
    }
    return true;
}

namespace {

// Independent replay of the recorded DPLL tree. It trusts only the CNF and the
// recorded steps: every propagation must be genuinely forced, every conflict
// clause must be falsified, propagation must have reached a fixpoint before a
// decision, and both polarities must be explored to prove UNSAT.
class ProofChecker {
public:
    ProofChecker(const CNF& cnf, const SolveResult& r) : cnf_(cnf), r_(r) {}

    // Verifies the whole tree and stores the root verdict (1 SAT, 0 UNSAT).
    bool run(int& rootVerdict, std::string& error) {
        if (r_.rootNode < 0 || r_.rootNode >= static_cast<int>(r_.proof.size())) {
            error = "invalid root node id";
            return false;
        }
        std::vector<int> seen(r_.proof.size(), 0);
        int verdict = checkNode(r_.rootNode, std::vector<int>(cnf_.numVars + 1, 0),
                                seen, error);
        if (verdict == -1) return false;
        for (size_t i = 0; i < seen.size(); ++i) {
            if (!seen[i]) {
                error = "node " + std::to_string(i) + " is unreachable from root";
                return false;
            }
        }
        rootVerdict = verdict;
        return true;
    }

    // Returns: 1 = subtree proves SAT, 0 = subtree proves UNSAT, -1 = error.
    int checkNode(int id, std::vector<int> a, std::vector<int>& seen,
                  std::string& error) {
        if (id < 0 || id >= static_cast<int>(r_.proof.size())) {
            error = "child references missing node " + std::to_string(id);
            return -1;
        }
        if (seen[id]) {
            error = "node " + std::to_string(id) + " visited twice (tree is cyclic/shared)";
            return -1;
        }
        seen[id] = 1;
        const ProofNode& n = r_.proof[id];

        for (const PropStep& p : n.propagations) {
            int var = std::abs(p.literal);
            if (var < 1 || var > cnf_.numVars) {
                error = "node " + std::to_string(id) + ": propagated literal out of range";
                return -1;
            }
            if (a[var] != 0) {
                error = "node " + std::to_string(id) +
                        ": literal assigned twice during replay";
                return -1;
            }
            if (p.reasonClause < 0 ||
                p.reasonClause >= static_cast<int>(cnf_.clauses.size())) {
                error = "node " + std::to_string(id) + ": invalid reason clause index";
                return -1;
            }
            const std::vector<int>& clause = cnf_.clauses[p.reasonClause];
            int unassigned = 0;
            int forced = 0;
            bool satisfied = false;
            for (int lit : clause) {
                int val = literalValue(lit, a);
                if (val == 1) { satisfied = true; break; }
                if (val == 0) { ++unassigned; forced = lit; }
            }
            if (satisfied) {
                error = "node " + std::to_string(id) +
                        ": propagation reason clause already satisfied";
                return -1;
            }
            if (unassigned != 1) {
                error = "node " + std::to_string(id) +
                        ": propagation reason clause is not unit (" +
                        std::to_string(unassigned) + " unassigned literals)";
                return -1;
            }
            if (forced != p.literal) {
                error = "node " + std::to_string(id) +
                        ": recorded literal does not match the unit literal";
                return -1;
            }
            a[var] = p.literal > 0 ? 1 : -1;
        }

        // Count variables still unassigned after the recorded propagations.
        int unassignedCount = 0;
        for (int v = 1; v <= cnf_.numVars; ++v)
            if (a[v] == 0) ++unassignedCount;

        auto clauseState = [&](int ci, int& unassigned) {
            unassigned = 0;
            for (int lit : cnf_.clauses[ci]) {
                int val = literalValue(lit, a);
                if (val == 1) return true;
                if (val == 0) ++unassigned;
            }
            return false;
        };

        if (n.kind == NODE_CONFLICT) {
            if (n.conflictClause < 0 ||
                n.conflictClause >= static_cast<int>(cnf_.clauses.size())) {
                error = "node " + std::to_string(id) + ": invalid conflict clause";
                return -1;
            }
            for (int lit : cnf_.clauses[n.conflictClause]) {
                if (literalValue(lit, a) != -1) {
                    error = "node " + std::to_string(id) +
                            ": recorded conflict clause is not fully falsified";
                    return -1;
                }
            }
            if (n.positiveChild != -1 || n.negativeChild != -1) {
                error = "node " + std::to_string(id) + ": conflict leaf has children";
                return -1;
            }
            return 0;
        }

        if (n.kind == NODE_SAT) {
            if (unassignedCount != 0) {
                error = "node " + std::to_string(id) + ": SAT leaf has unassigned variables";
                return -1;
            }
            for (size_t ci = 0; ci < cnf_.clauses.size(); ++ci) {
                int unassigned = 0;
                if (!clauseState(static_cast<int>(ci), unassigned)) {
                    error = "node " + std::to_string(id) +
                            ": SAT leaf falsifies clause " + std::to_string(ci);
                    return -1;
                }
            }
            if (static_cast<int>(n.satModel.size()) != cnf_.numVars + 1 ||
                !std::equal(n.satModel.begin() + 1, n.satModel.end(), a.begin() + 1)) {
                error = "node " + std::to_string(id) + ": recorded model disagrees with replay";
                return -1;
            }
            return 1;
        }

        // Branch node.
        if (n.decisionVar < 1 || n.decisionVar > cnf_.numVars ||
            a[n.decisionVar] != 0) {
            error = "node " + std::to_string(id) + ": invalid decision variable";
            return -1;
        }
        // A sound branch needs no falsified clause and no unit clause remaining.
        for (size_t ci = 0; ci < cnf_.clauses.size(); ++ci) {
            int unassigned = 0;
            bool satisfied = clauseState(static_cast<int>(ci), unassigned);
            if (!satisfied && unassigned == 0) {
                error = "node " + std::to_string(id) +
                        ": clause " + std::to_string(ci) +
                        " falsified at branch node but no conflict was recorded";
                return -1;
            }
            if (!satisfied && unassigned == 1) {
                error = "node " + std::to_string(id) +
                        ": unit clause " + std::to_string(ci) +
                        " remained before branching";
                return -1;
            }
        }

        if (n.positiveChild == -1) {
            error = "node " + std::to_string(id) + ": missing positive child";
            return -1;
        }
        std::vector<int> aPos = a;
        aPos[n.decisionVar] = 1;
        int pos = checkNode(n.positiveChild, aPos, seen, error);
        if (pos == -1) return -1;
        if (pos == 1) return 1;  // one feasible branch suffices for SAT

        if (n.negativeChild == -1) {
            error = "node " + std::to_string(id) +
                    ": positive branch failed but negative branch is missing";
            return -1;
        }
        std::vector<int> aNeg = a;
        aNeg[n.decisionVar] = -1;
        int neg = checkNode(n.negativeChild, aNeg, seen, error);
        if (neg == -1) return -1;
        if (neg == 1) return 1;
        return 0;  // both branches infeasible => UNSAT
    }

private:
    const CNF& cnf_;
    const SolveResult& r_;
};

}  // namespace

bool verifyProof(const CNF& cnf, const SolveResult& r, std::string& error) {
    if (r.status == SolveResult::LIMIT) {
        error = "cannot verify: search hit the node limit";
        return false;
    }
    ProofChecker checker(cnf, r);
    int rootVerdict = -1;
    if (!checker.run(rootVerdict, error)) return false;
    const bool claimsSat = (r.status == SolveResult::SAT);
    if (claimsSat != (rootVerdict == 1)) {
        error = claimsSat
                    ? "claimed SAT but replay proves the recorded tree UNSAT"
                    : "claimed UNSAT but replay finds a satisfying leaf";
        return false;
    }
    if (claimsSat) {
        if (static_cast<int>(r.model.size()) != cnf.numVars + 1) {
            error = "top-level model has wrong size";
            return false;
        }
        int falsified = -1;
        if (!modelSatisfies(cnf, r.model, falsified)) {
            error = "top-level model falsifies clause " + std::to_string(falsified);
            return false;
        }
    }
    return true;
}

}  // namespace sat
