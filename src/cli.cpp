#include <chrono>
#include <cstdio>
#include <fstream>
#include <iostream>
#include <random>
#include <sstream>
#include <string>

#include "graph.hpp"
#include "json.hpp"
#include "treewidth.hpp"
#include "validate.hpp"

namespace {

using json::Value;

Value edgePairJson(int u, int v, const Graph& g) {
    return Value::ArrayT{Value(g.labels[u]), Value(g.labels[v])};
}

Value errorEnvelope(const std::string& msg) {
    return Value(Value::ObjectT{
        {"ok", Value(false)},
        {"error", Value(msg)},
    });
}

Value labelsArray(const std::vector<int>& verts, const Graph& g) {
    Value::ArrayT out;
    for (int v : verts) out.push_back(Value(g.labels[v]));
    return Value(std::move(out));
}

Value eliminationToJson(const EliminationResult& r, const Graph& g) {
    Value::ArrayT steps;
    for (const ElimStep& s : r.steps) {
        Value::ArrayT fill;
        for (auto [u, v] : s.addedFill) fill.push_back(edgePairJson(u, v, g));
        Value::ObjectT step;
        step.emplace("position", Value(s.position));
        step.emplace("vertex", Value(g.labels[s.vertex]));
        step.emplace("degree_at_elimination", Value(s.degreeAtElimination));
        step.emplace("bag", labelsArray(s.bag, g));
        step.emplace("added_fill", Value(std::move(fill)));
        steps.push_back(Value(std::move(step)));
    }
    Value::ObjectT out;
    out.emplace("heuristic", Value(r.heuristic));
    out.emplace("order", labelsArray(r.order, g));
    out.emplace("steps", Value(std::move(steps)));
    out.emplace("heuristic_width", Value(r.width));
    out.emplace("total_fill", Value(static_cast<long long>(r.totalFill)));
    out.emplace("elapsed_ms", Value(r.elapsedMs));
    out.emplace(
        "note",
        Value("heuristic_width is an upper bound on treewidth, NOT claimed "
              "optimal"));
    return Value(std::move(out));
}

Value decompositionToJson(const TreeDecomposition& td, const Graph& g) {
    Value::ArrayT bags;
    for (const TDBag& b : td.bags) {
        Value::ObjectT bag;
        bag.emplace("id", Value(b.id));
        bag.emplace("vertices", labelsArray(b.vertices, g));
        bag.emplace("size", Value(static_cast<int>(b.vertices.size())));
        bag.emplace("parent",
                    b.parent < 0 ? Value(nullptr) : Value(b.parent));
        bags.push_back(Value(std::move(bag)));
    }
    auto edgesJson = [&](const std::vector<std::pair<int, int>>& es) {
        Value::ArrayT out;
        for (auto [a, b] : es)
            out.push_back(Value::ArrayT{Value(a), Value(b)});
        return Value(std::move(out));
    };
    Value::ObjectT out;
    out.emplace("bags", Value(std::move(bags)));
    out.emplace("tree_edges", edgesJson(td.treeEdges));
    out.emplace("root_join_edges", edgesJson(td.rootJoinEdges));
    out.emplace("width", Value(td.width));
    out.emplace("component_roots", Value(td.roots));
    return Value(std::move(out));
}

Value verificationToJson(const VerificationResult& v) {
    Value::ArrayT errs, warns;
    for (const auto& e : v.errors) errs.push_back(Value(e));
    for (const auto& w : v.warnings) warns.push_back(Value(w));
    return Value(Value::ObjectT{
        {"passed", Value(v.ok)},
        {"width_recomputed", Value(v.width)},
        {"bag_count", Value(static_cast<long long>(v.bagCount))},
        {"edges_covered", Value(v.edgesCovered)},
        {"edges_total", Value(v.edgesTotal)},
        {"tree_connected", Value(v.treeConnected)},
        {"errors", Value(std::move(errs))},
        {"warnings", Value(std::move(warns))},
    });
}

Value exactToJson(const ExactResult& e, const Graph& g) {
    if (e.hitHardLimit)
        return Value(Value::ObjectT{
            {"feasible", Value(false)},
            {"limit_exceeded", Value(true)},
            {"hard_limit", Value(MAX_EXACT_N_HARD)},
        });
    if (!e.feasible)
        return Value(Value::ObjectT{
            {"feasible", Value(false)},
            {"limit_exceeded", Value(false)},
            {"note",
             Value("graph larger than requested exact limit; skipped")},
        });
    return Value(Value::ObjectT{
        {"feasible", Value(true)},
        {"optimal_treewidth", Value(e.width)},
        {"optimal_order", labelsArray(e.order, g)},
        {"memo_states", Value(e.memoStates)},
        {"search_nodes", Value(e.searchNodes)},
        {"elapsed_ms", Value(e.elapsedMs)},
    });
}

Value naiveToJson(const NaiveResult& n, const Graph& g) {
    if (g.n() > MAX_NAIVE_N)
        return Value(Value::ObjectT{
            {"feasible", Value(false)},
            {"limit", Value(MAX_NAIVE_N)},
        });
    return Value(Value::ObjectT{
        {"feasible", Value(true)},
        {"optimal_treewidth", Value(n.width)},
        {"optimal_order", labelsArray(n.order, g)},
        {"permutations_checked", Value(n.permutations)},
        {"elapsed_ms", Value(n.elapsedMs)},
    });
}

// Check the heuristic result against the independently computed optimum and
// against the width of the heuristic order itself.
Value comparisonToJson(const Graph& g, const EliminationResult& elim,
                       const ExactResult& exact) {
    Value::ObjectT out;
    int independentWidth = eliminationWidth(g, elim.order);
    out["heuristic_order_width_recomputed"] = Value(independentWidth);
    out["heuristic_matches_replay"] = Value(independentWidth == elim.width);
    if (exact.feasible) {
        out["optimal_treewidth"] = Value(exact.width);
        out["heuristic_is_optimal"] = Value(elim.width == exact.width);
        out["gap"] = Value(elim.width - exact.width);
    }
    return Value(std::move(out));
}

Value actionSolve(const Value& req) {
    Graph g = graphFromJson(req.contains("graph") ? req["graph"] : req);
    if (g.n() > MAX_VERTICES)
        throw std::runtime_error(
            "graph has " + std::to_string(g.n()) +
            " vertices; heuristic limit is " + std::to_string(MAX_VERTICES));

    std::string heuristic =
        req.contains("heuristic") ? req["heuristic"].asString() : "min-fill";
    if (heuristic != "min-fill" && heuristic != "min-degree")
        throw std::runtime_error(
            "unknown heuristic (use 'min-fill' or 'min-degree')");
    EliminationResult elim =
        heuristic == "min-degree" ? minDegreeOrder(g) : minFillOrder(g);

    TreeDecomposition td = buildTreeDecomposition(g, elim.order);
    VerificationResult ver = verifyDecomposition(g, td);

    std::vector<std::string> elimErrors;
    bool elimOk = verifyElimination(g, elim, &elimErrors);
    bool bagsOk = verifyBagsMatchOrder(g, elim.order, td, &elimErrors);

    int exactLimit = MAX_EXACT_N_DEFAULT;
    if (req.contains("exact_limit")) {
        if (!req["exact_limit"].isInt())
            throw std::runtime_error("exact_limit must be an integer");
        exactLimit = static_cast<int>(req["exact_limit"].asInt());
    }
    ExactResult exact = exactOptimal(g, exactLimit);

    Value::ObjectT data;
    data["input"] = graphToJson(g);
    data["n"] = Value(g.n());
    data["m"] = Value(g.edgeCount());
    data["elimination"] = eliminationToJson(elim, g);
    data["tree_decomposition"] = decompositionToJson(td, g);
    data["verification"] = verificationToJson(ver);
    Value::ArrayT elimErrs;
    for (const auto& e : elimErrors) elimErrs.push_back(Value(e));
    data["elimination_self_check"] = Value(Value::ObjectT{
        {"passed", Value(elimOk && bagsOk)},
        {"errors", Value(std::move(elimErrs))},
    });
    data["exact"] = exactToJson(exact, g);
    data["comparison"] = comparisonToJson(g, elim, exact);
    data["terminology"] =
        Value("optimal_treewidth is populated only by exhaustive/memoized "
              "search on small graphs; heuristic_width is never labeled "
              "optimal");
    return Value(std::move(data));
}

Value actionExact(const Value& req) {
    Graph g = graphFromJson(req.contains("graph") ? req["graph"] : req);
    int limit = req.contains("limit")
                    ? static_cast<int>(req["limit"].asInt())
                    : MAX_EXACT_N_DEFAULT;
    ExactResult e = exactOptimal(g, limit);
    Value::ObjectT data;
    data["n"] = Value(g.n());
    data["result"] = exactToJson(e, g);
    return Value(std::move(data));
}

Value actionNaive(const Value& req) {
    Graph g = graphFromJson(req.contains("graph") ? req["graph"] : req);
    if (g.n() > MAX_NAIVE_N)
        throw std::runtime_error(
            "naive reference supports at most " +
            std::to_string(MAX_NAIVE_N) + " vertices");
    NaiveResult n = naiveOptimal(g);
    Value::ObjectT data;
    data["n"] = Value(g.n());
    data["result"] = naiveToJson(n, g);
    return Value(std::move(data));
}

Value actionGenerate(const Value& req) {
    if (!req.contains("n") || !req["n"].isInt())
        throw std::runtime_error("generate requires integer 'n'");
    int n = static_cast<int>(req["n"].asInt());
    double p = req.contains("p") ? req["p"].asDouble() : 0.3;
    unsigned seed = req.contains("seed")
                        ? static_cast<unsigned>(req["seed"].asInt())
                        : std::random_device{}();
    if (n < 0 || n > MAX_VERTICES)
        throw std::runtime_error("n out of range");
    if (p < 0.0 || p > 1.0)
        throw std::runtime_error("p must be in [0,1]");

    std::mt19937 rng(seed);
    std::bernoulli_distribution dist(p);
    Value::ArrayT verts;
    Value::ArrayT edges;
    for (int i = 0; i < n; ++i) verts.push_back(Value(std::to_string(i)));
    for (int u = 0; u < n; ++u)
        for (int v = u + 1; v < n; ++v)
            if (dist(rng))
                edges.push_back(Value::ArrayT{Value(u), Value(v)});
    Value::ObjectT graph;
    graph.emplace("vertices", Value(std::move(verts)));
    graph.emplace("edges", Value(std::move(edges)));
    Value::ObjectT out;
    out.emplace("seed", Value(static_cast<long long>(seed)));
    out.emplace("graph", Value(std::move(graph)));
    return Value(std::move(out));
}

Value dispatch(const Value& req) {
    if (!req.isObject())
        throw std::runtime_error("request must be a JSON object");
    std::string action =
        req.contains("action") ? req["action"].asString() : "solve";
    auto t0 = std::chrono::steady_clock::now();
    Value data;
    if (action == "solve") data = actionSolve(req);
    else if (action == "exact") data = actionExact(req);
    else if (action == "naive") data = actionNaive(req);
    else if (action == "generate") data = actionGenerate(req);
    else throw std::runtime_error("unknown action: " + action);

    double wallMs =
        std::chrono::duration<double, std::milli>(
            std::chrono::steady_clock::now() - t0)
            .count();
    return Value(Value::ObjectT{
        {"ok", Value(true)},
        {"action", Value(action)},
        {"wall_ms", Value(wallMs)},
        {"data", std::move(data)},
    });
}

void printUsage() {
    std::cerr
        << "treewidth — heuristic tree decomposition backend (no external "
           "solver)\n\n"
        << "Usage:\n"
        << "  treewidth [--input FILE] [--compact]\n"
        << "  treewidth --help | --version\n\n"
        << "Reads one JSON request (or a JSON array of requests) from stdin "
           "or --input,\n"
        << "writes one JSON response per request to stdout.\n\n"
        << "Actions: solve (default), exact, naive, generate.\n"
        << "Graph: {\"vertices\":[...], \"edges\":[[u,v],...]} or "
           "{\"n\":k,\"edges\":[[i,j]]}\n"
        << "       or {\"adjacency\": {\"a\": [\"b\", ...]}}.\n\n"
        << "Limits: heuristic n <= " << MAX_VERTICES
        << ", exact n <= " << MAX_EXACT_N_DEFAULT << " (hard "
        << MAX_EXACT_N_HARD << "), naive n! enumeration n <= " << MAX_NAIVE_N
        << ".\n";
}

}  // namespace

int main(int argc, char** argv) {
    std::string inputPath;
    int indent = 2;
    for (int i = 1; i < argc; ++i) {
        std::string arg = argv[i];
        if (arg == "--help" || arg == "-h") {
            printUsage();
            return 0;
        }
        if (arg == "--version") {
            std::cout << "treewidth 1.0.0\n";
            return 0;
        }
        if (arg == "--input" && i + 1 < argc) {
            inputPath = argv[++i];
        } else if (arg == "--compact") {
            indent = 0;
        } else {
            std::cerr << "unknown argument: " << arg << "\n";
            printUsage();
            return 2;
        }
    }

    std::istream* in = &std::cin;
    std::ifstream file;
    if (!inputPath.empty()) {
        file.open(inputPath);
        if (!file) {
            std::cerr << "cannot open input file: " << inputPath << "\n";
            return 2;
        }
        in = &file;
    }
    std::stringstream ss;
    ss << in->rdbuf();

    Value response;
    try {
        Value req = json::parse(ss.str());
        if (req.isArray()) {
            Value::ArrayT results;
            for (const Value& one : req.asArray()) {
                try {
                    results.push_back(dispatch(one));
                } catch (const std::exception& ex) {
                    results.push_back(errorEnvelope(ex.what()));
                }
            }
            response = Value(std::move(results));
        } else {
            response = dispatch(req);
        }
    } catch (const std::exception& ex) {
        response = errorEnvelope(ex.what());
    }
    std::cout << json::dump(response, indent);
    return response.isObject() && response.contains("ok") &&
                   !response["ok"].asBool()
               ? 1
               : 0;
}
