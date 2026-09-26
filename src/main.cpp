// Command line interface for the small-scale SAT solver.
//
//   sat_solver solve <file.cnf|-> [node_limit]   DPLL, prints JSON result+proof
//   sat_solver brute <file.cnf|->                naive exhaustive reference
//   sat_solver verify <result.json|->            independent proof replay
//   sat_solver serve [port]                      HTTP JSON API (default 8080)
#include <fstream>
#include <iostream>
#include <iterator>
#include <sstream>
#include <string>

#include "server.hpp"
#include "solver.hpp"

namespace {

std::string readAll(const std::string& path) {
    if (path == "-") {
        return std::string(std::istreambuf_iterator<char>(std::cin),
                           std::istreambuf_iterator<char>());
    }
    std::ifstream in(path);
    if (!in) {
        throw std::runtime_error("cannot open file: " + path);
    }
    return std::string(std::istreambuf_iterator<char>(in),
                       std::istreambuf_iterator<char>());
}

int usage() {
    std::cerr
        << "usage:\n"
        << "  sat_solver solve <file.cnf|-> [node_limit]  DPLL with proof (JSON out)\n"
        << "  sat_solver brute <file.cnf|->               naive exhaustive reference\n"
        << "  sat_solver verify <result.json|->           replay & check a proof\n"
        << "  sat_solver serve [port]                     HTTP JSON API (default 8080)\n";
    return 2;
}

int cmdSolve(const std::string& path, const std::string& limitArg) {
    sat::ParseResult pr = sat::parseDimacs(readAll(path));
    if (!pr.ok) {
        std::cerr << "parse error: " << pr.error << "\n";
        return 1;
    }
    sat::normalizeCnf(pr.cnf);
    if (pr.cnf.numVars > sat::MAX_DPLL_VARS) {
        std::cerr << "error: num_vars exceeds DPLL limit of " << sat::MAX_DPLL_VARS
                  << "\n";
        return 1;
    }
    sat::SolveOptions opts;
    if (!limitArg.empty()) {
        try {
            opts.nodeLimit = std::stoull(limitArg);
        } catch (...) {
            std::cerr << "error: invalid node limit\n";
            return 1;
        }
    }
    sat::SolveResult r = sat::dpll(pr.cnf, opts);
    std::cout << minijson::dump(sat::resultToJson(r, pr.cnf)) << "\n";
    return r.status == sat::SolveResult::LIMIT ? 3 : 0;
}

int cmdBrute(const std::string& path) {
    sat::ParseResult pr = sat::parseDimacs(readAll(path));
    if (!pr.ok) {
        std::cerr << "parse error: " << pr.error << "\n";
        return 1;
    }
    sat::normalizeCnf(pr.cnf);
    sat::BruteResult br;
    std::string err;
    if (!sat::bruteForce(pr.cnf, br, err)) {
        std::cerr << "error: " << err << "\n";
        return 1;
    }
    minijson::Value out = minijson::Value::makeObj();
    out.set("status", minijson::Value::makeStr(br.sat ? "sat" : "unsat"));
    out.set("assignments_tested",
            minijson::Value::makeInt(static_cast<long long>(br.tested)));
    if (br.sat) {
        minijson::Value model = minijson::Value::makeArr();
        for (int v = 1; v <= pr.cnf.numVars; ++v) {
            minijson::Value entry = minijson::Value::makeObj();
            entry.set("variable", minijson::Value::makeInt(v));
            entry.set("value", minijson::Value::makeBool(br.model[v] == 1));
            model.arr.push_back(std::move(entry));
        }
        out.set("model", std::move(model));
    }
    std::cout << minijson::dump(out) << "\n";
    return 0;
}

int cmdVerify(const std::string& path) {
    minijson::Value doc;
    std::string err;
    if (!minijson::parse(readAll(path), doc, err)) {
        std::cerr << "invalid JSON: " << err << "\n";
        return 1;
    }
    // The result document carries the normalized clauses; rebuild the CNF.
    const minijson::Value* numVars = doc.find("num_vars");
    const minijson::Value* clauses = doc.find("normalized_clauses");
    if (numVars == nullptr || numVars->type != minijson::Value::INT ||
        clauses == nullptr || clauses->type != minijson::Value::ARR) {
        std::cerr << "error: document lacks \"num_vars\"/\"normalized_clauses\"\n";
        return 1;
    }
    sat::CNF cnf;
    cnf.numVars = static_cast<int>(numVars->i);
    for (const minijson::Value& jc : clauses->arr) {
        if (jc.type != minijson::Value::ARR) {
            std::cerr << "error: malformed normalized_clauses\n";
            return 1;
        }
        std::vector<int> clause;
        for (const minijson::Value& lit : jc.arr) {
            if (lit.type != minijson::Value::INT) {
                std::cerr << "error: malformed literal in normalized_clauses\n";
                return 1;
            }
            clause.push_back(static_cast<int>(lit.i));
        }
        cnf.clauses.push_back(std::move(clause));
    }
    sat::SolveResult r;
    if (!sat::jsonToResult(doc, r, err)) {
        std::cerr << "error: " << err << "\n";
        return 1;
    }
    if (sat::verifyProof(cnf, r, err)) {
        std::cout << "{\"valid\":true}\n";
        return 0;
    }
    minijson::Value out = minijson::Value::makeObj();
    out.set("valid", minijson::Value::makeBool(false));
    out.set("reason", minijson::Value::makeStr(err));
    std::cout << minijson::dump(out) << "\n";
    return 1;
}

}  // namespace

int main(int argc, char** argv) {
    try {
        if (argc < 2) return usage();
        std::string cmd = argv[1];
        if (cmd == "solve" && (argc == 3 || argc == 4)) {
            return cmdSolve(argv[2], argc == 4 ? argv[3] : "");
        }
        if (cmd == "brute" && argc == 3) return cmdBrute(argv[2]);
        if (cmd == "verify" && argc == 3) return cmdVerify(argv[2]);
        if (cmd == "serve" && (argc == 2 || argc == 3)) {
            int port = argc == 3 ? std::stoi(argv[2]) : 8080;
            return sat::runServer(port);
        }
        return usage();
    } catch (const std::exception& e) {
        std::cerr << "fatal: " << e.what() << "\n";
        return 1;
    }
}
