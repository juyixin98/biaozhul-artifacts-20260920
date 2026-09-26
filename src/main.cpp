#include "graph_builder.hpp"
#include "json.hpp"
#include "naive.hpp"
#include "scc.hpp"
#include "verify.hpp"

#include <chrono>
#include <fstream>
#include <iostream>
#include <sstream>
#include <string>

namespace {

void printUsage() {
    std::cerr <<
        "usage:\n"
        "  scc_analyze [--pretty] [request.json]\n"
        "  cat request.json | scc_analyze [--pretty]\n"
        "\n"
        "Reads a directed-graph request, computes SCCs + condensation DAG,\n"
        "verifies the result independently, and writes one JSON document.\n"
        "Exit code 0 on success, 2 on invalid request, 1 on internal error.\n";
}

json::Value labelsArray(const std::vector<int>& vertices, const std::vector<std::string>& labels) {
    json::Value arr = json::Value::makeArray();
    for (int v : vertices) arr.push(json::Value::fromString(labels[static_cast<std::size_t>(v)]));
    return arr;
}

json::Value intArray(const std::vector<int>& vertices) {
    json::Value arr = json::Value::makeArray();
    for (int v : vertices) arr.push(json::Value::fromInt(v));
    return arr;
}

json::Value matrixRows(const std::vector<char>& matrix, int n) {
    json::Value rows = json::Value::makeArray();
    for (int u = 0; u < n; ++u) {
        json::Value row = json::Value::makeArray();
        for (int v = 0; v < n; ++v)
            row.push(json::Value::fromInt(
                matrix[static_cast<std::size_t>(u) * static_cast<std::size_t>(n) +
                       static_cast<std::size_t>(v)] != 0 ? 1 : 0));
        rows.push(std::move(row));
    }
    return rows;
}

} // namespace

int main(int argc, char** argv) {
    bool pretty = false;
    std::string inputPath;
    for (int i = 1; i < argc; ++i) {
        std::string arg = argv[i];
        if (arg == "--pretty") pretty = true;
        else if (arg == "-h" || arg == "--help") { printUsage(); return 0; }
        else if (!arg.empty() && arg[0] == '-') {
            std::cerr << "unknown option: " << arg << "\n";
            printUsage();
            return 2;
        } else if (inputPath.empty()) {
            inputPath = arg;
        } else {
            std::cerr << "unexpected argument: " << arg << "\n";
            return 2;
        }
    }

    std::string text;
    try {
        if (inputPath.empty() || inputPath == "-") {
            std::ostringstream ss;
            ss << std::cin.rdbuf();
            text = ss.str();
        } else {
            std::ifstream in(inputPath);
            if (!in) {
                std::cerr << "cannot open input file: " << inputPath << "\n";
                return 2;
            }
            std::ostringstream ss;
            ss << in.rdbuf();
            text = ss.str();
        }
    } catch (const std::exception& ex) {
        std::cerr << "failed to read input: " << ex.what() << "\n";
        return 1;
    }

    auto errorResponse = [&](const std::string& message) {
        json::Value out = json::Value::makeObject();
        out.set("ok", json::Value::fromBool(false));
        out.set("error", json::Value::fromString(message));
        std::cout << out.dumpPretty(2) << "\n";
    };

    json::Value request;
    try {
        request = json::Value::parse(text);
    } catch (const std::exception& ex) {
        errorResponse(ex.what());
        return 2;
    }

    Graph graph;
    try {
        graph = buildGraph(request);
    } catch (const std::exception& ex) {
        errorResponse(ex.what());
        return 2;
    }
    // Release the parsed request tree and raw input before solving: for inputs
    // near the size limit this frees hundreds of MB of transient allocations.
    request = json::Value();
    std::string().swap(text);

    const auto t0 = std::chrono::steady_clock::now();
    AnalysisResult result = scc::analyze(graph);
    const auto t1 = std::chrono::steady_clock::now();
    verify::CheckReport report = verify::checkResult(graph, result);
    const auto t2 = std::chrono::steady_clock::now();

    json::Value out = json::Value::makeObject();
    out.set("ok", json::Value::fromBool(report.ok));

    // Echo-back of the normalized graph.
    json::Value echoVertices = json::Value::makeArray();
    for (const auto& label : graph.labels) echoVertices.push(json::Value::fromString(label));
    out.set("vertices", std::move(echoVertices));

    // Stats.
    json::Value stats = json::Value::makeObject();
    stats.set("n", json::Value::fromInt(graph.n));
    stats.set("rawEdgeCount", json::Value::fromInt(graph.totalRawEdges));
    stats.set("uniqueEdgeCount", json::Value::fromInt(static_cast<std::int64_t>(graph.uniqueEdges.size())));
    stats.set("componentCount", json::Value::fromInt(static_cast<std::int64_t>(result.components.size())));
    stats.set("dagEdgeCount", json::Value::fromInt(static_cast<std::int64_t>(result.dagEdges.size())));
    stats.set("solverMicros",
              json::Value::fromInt(std::chrono::duration_cast<std::chrono::microseconds>(t1 - t0).count()));
    stats.set("verifyMicros",
              json::Value::fromInt(std::chrono::duration_cast<std::chrono::microseconds>(t2 - t1).count()));
    out.set("stats", std::move(stats));

    // Components with witnesses.
    json::Value comps = json::Value::makeArray();
    for (const Component& c : result.components) {
        json::Value co = json::Value::makeObject();
        co.set("id", json::Value::fromInt(c.id));
        co.set("representative", json::Value::fromInt(c.representative));
        co.set("vertices", intArray(c.vertices));
        co.set("vertexLabels", labelsArray(c.vertices, graph.labels));
        const std::vector<int>& w = result.witnesses[static_cast<std::size_t>(c.id)];
        co.set("cyclic", json::Value::fromBool(!w.empty()));
        co.set("cycleWitness", intArray(w)); // closed walk v0..vk with implicit edge vk->v0
        co.set("cycleWitnessLabels", labelsArray(w, graph.labels));
        comps.push(std::move(co));
    }
    out.set("components", std::move(comps));

    // Condensation DAG edges (already deduped and deterministically ordered).
    json::Value dag = json::Value::makeObject();
    json::Value edges = json::Value::makeArray();
    for (const DagEdge& d : result.dagEdges) {
        json::Value eo = json::Value::makeObject();
        eo.set("from", json::Value::fromInt(d.fromComponent));
        eo.set("to", json::Value::fromInt(d.toComponent));
        eo.set("multiplicity", json::Value::fromInt(d.multiplicity));
        edges.push(std::move(eo));
    }
    dag.set("edges", std::move(edges));
    out.set("condensation", std::move(dag));

    // Unique edges of the input multigraph (dedup evidence).
    json::Value unique = json::Value::makeArray();
    for (const Edge& e : graph.uniqueEdges) {
        json::Value eo = json::Value::makeObject();
        eo.set("from", json::Value::fromInt(e.from));
        eo.set("to", json::Value::fromInt(e.to));
        eo.set("multiplicity", json::Value::fromInt(e.multiplicity));
        unique.push(std::move(eo));
    }
    out.set("uniqueEdges", std::move(unique));

    // Naive small-graph reference evidence.
    json::Value ref = json::Value::makeObject();
    bool naiveEnabled = graph.n <= limits::MAX_NAIVE_N;
    ref.set("enabled", json::Value::fromBool(naiveEnabled));
    ref.set("cutoff", json::Value::fromInt(limits::MAX_NAIVE_N));
    if (naiveEnabled) {
        std::vector<char> reach = naive::reachabilityMatrix(graph);
        ref.set("reachabilityMatrix", matrixRows(reach, graph.n));
        std::vector<int> naiveComp = naive::sccByReachability(graph);
        bool partitionMatches = true;
        for (int v = 0; v < graph.n; ++v)
            if (naiveComp[static_cast<std::size_t>(v)] != result.compOf[static_cast<std::size_t>(v)])
                partitionMatches = false;
        ref.set("sccPartitionMatchesNaive", json::Value::fromBool(partitionMatches));
        json::Value naiveIds = json::Value::makeArray();
        for (int c : naiveComp) naiveIds.push(json::Value::fromInt(c));
        ref.set("naiveComponentOf", std::move(naiveIds));
    }
    out.set("naiveReference", std::move(ref));

    // Independent verification report.
    json::Value ver = json::Value::makeObject();
    ver.set("ok", json::Value::fromBool(report.ok));
    json::Value failures = json::Value::makeArray();
    for (const auto& msg : report.failures) failures.push(json::Value::fromString(msg));
    ver.set("failures", std::move(failures));
    out.set("verification", std::move(ver));

    std::cout << (pretty ? out.dumpPretty(2) : out.dump()) << "\n";
    return report.ok ? 0 : 1;
}
