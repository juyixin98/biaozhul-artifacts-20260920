// Line-delimited JSON interface to the incremental topological-order engine.
//
// Each stdin line is one request object; each stdout line is one response.
// No third-party libraries: the JSON parser is src/json.cpp.

#include <cerrno>
#include <cmath>
#include <cstdint>
#include <iostream>
#include <limits>
#include <string>

#include "json.hpp"
#include "topo.hpp"

namespace {

using json::Value;
using topo::IncrementalTopo;
using topo::InsertResult;

Value errResponse(const std::string& msg) {
    Value r = Value::object();
    r.set("ok", Value(false));
    r.set("error", Value(msg));
    return r;
}

// Reads a finite integral JSON number fitting in int.
bool intFromValue(const Value& v, int& out) {
    if (!v.isNumber()) return false;
    double d = v.asNumber();
    if (!std::isfinite(d) || d != std::floor(d)) return false;
    if (d < static_cast<double>(std::numeric_limits<int>::min()) ||
        d > static_cast<double>(std::numeric_limits<int>::max())) {
        return false;
    }
    out = static_cast<int>(d);
    return true;
}

bool readIntId(const Value& req, const char* key, int& out) {
    const Value* v = req.find(key);
    return v != nullptr && intFromValue(*v, out);
}

Value idsArray(const std::vector<int>& ids) {
    Value arr = Value::array();
    for (int id : ids) arr.push_back(Value(static_cast<int64_t>(id)));
    return arr;
}

Value insertResultJson(int u, int v, const InsertResult& r) {
    Value out = Value::object();
    out.set("u", Value(static_cast<int64_t>(u)));
    out.set("v", Value(static_cast<int64_t>(v)));
    out.set("ok", Value(r.ok));
    out.set("duplicated", Value(r.duplicated));
    out.set("visited", Value(r.visited));
    if (r.cycle) {
        out.set("cycle", Value(true));
        out.set("cycle_path", idsArray(r.cyclePath));
    } else {
        out.set("cycle", Value(false));
    }
    if (r.rejected) {
        out.set("rejected", Value(true));
        out.set("error", Value(r.error));
    }
    return out;
}

Value statsJson(const IncrementalTopo& g, int64_t requests, int64_t rejected) {
    Value s = Value::object();
    s.set("nodes", Value(static_cast<int64_t>(g.numNodes())));
    s.set("edges", Value(g.numEdges()));
    s.set("insert_requests", Value(requests));
    s.set("rejected_requests", Value(rejected));
    s.set("reorders", Value(g.reorderedCount()));
    s.set("total_visited", Value(g.totalVisited()));
    return s;
}

// Applies one parsed request. Returns false when the session should terminate.
bool dispatch(IncrementalTopo& g, const Value& req, Value& resp,
              int64_t& requests, int64_t& rejected) {
    if (!req.isObject()) {
        resp = errResponse("request must be a JSON object");
        return true;
    }
    const Value* opv = req.find("op");
    if (opv == nullptr || !opv->isString()) {
        resp = errResponse("missing string field 'op'");
        return true;
    }
    const std::string& op = opv->asString();

    if (op == "quit" || op == "exit") {
        resp = Value::object();
        resp.set("ok", Value(true));
        return false;
    }

    if (op == "reset") {
        g = IncrementalTopo();
        requests = 0;
        rejected = 0;
        resp = Value::object();
        resp.set("ok", Value(true));
        return true;
    }

    if (op == "ping") {
        resp = Value::object();
        resp.set("ok", Value(true));
        resp.set("service", Value("incremental-topo"));
        return true;
    }

    if (op == "add_node") {
        int id = 0;
        if (!readIntId(req, "id", id)) {
            resp = errResponse("field 'id' must be a 32-bit integer");
            return true;
        }
        if (!g.hasNode(id) && g.numNodes() >= g.maxNodes()) {
            resp = errResponse("node limit exceeded");
            return true;
        }
        bool created = g.addNode(id);
        resp = Value::object();
        resp.set("ok", Value(true));
        resp.set("id", Value(static_cast<int64_t>(id)));
        resp.set("created", Value(created));
        return true;
    }

    if (op == "insert_edge") {
        int u = 0, v = 0;
        if (!readIntId(req, "u", u) || !readIntId(req, "v", v)) {
            resp = errResponse("fields 'u' and 'v' must be 32-bit integers");
            return true;
        }
        ++requests;
        InsertResult r = g.insertEdge(u, v);
        if (r.rejected) ++rejected;
        resp = insertResultJson(u, v, r);
        return true;
    }

    if (op == "batch") {
        const Value* edges = req.find("edges");
        if (edges == nullptr || !edges->isArray()) {
            resp = errResponse("field 'edges' must be an array of [u,v] pairs");
            return true;
        }
        Value results = Value::array();
        int failures = 0;
        int cycles = 0;
        int duplicates = 0;
        int64_t batchVisited = 0;
        for (const Value& e : edges->asArray()) {
            if (!e.isArray() || e.asArray().size() != 2) {
                results.push_back(errResponse("edge must be [u,v] integers"));
                ++failures;
                continue;
            }
            int u = 0, v = 0;
            if (!intFromValue(e.asArray()[0], u) ||
                !intFromValue(e.asArray()[1], v)) {
                results.push_back(errResponse("edge endpoints must be integers"));
                ++failures;
                continue;
            }
            ++requests;
            InsertResult r = g.insertEdge(u, v);
            if (r.rejected) ++rejected;
            if (r.cycle) ++cycles;
            if (r.duplicated) ++duplicates;
            if (!r.ok) ++failures;
            batchVisited += r.visited;
            results.push_back(insertResultJson(u, v, r));
        }
        resp = Value::object();
        resp.set("ok", Value(true));
        resp.set("results", results);
        resp.set("cycles", Value(static_cast<int64_t>(cycles)));
        resp.set("duplicates", Value(static_cast<int64_t>(duplicates)));
        resp.set("failed", Value(static_cast<int64_t>(failures)));
        resp.set("visited", Value(batchVisited));
        return true;
    }

    if (op == "order") {
        resp = Value::object();
        resp.set("ok", Value(true));
        resp.set("order", idsArray(g.order()));
        resp.set("nodes", Value(static_cast<int64_t>(g.numNodes())));
        return true;
    }

    if (op == "verify") {
        auto rep = g.verify();
        resp = Value::object();
        resp.set("ok", Value(true));
        resp.set("valid", Value(rep.valid));
        resp.set("permutation", Value(rep.permutation));
        resp.set("acyclic", Value(rep.acyclic));
        resp.set("kahn_order", idsArray(rep.kahnOrder));
        return true;
    }

    if (op == "stats") {
        resp = Value::object();
        resp.set("ok", Value(true));
        resp.set("stats", statsJson(g, requests, rejected));
        return true;
    }

    if (op == "limits") {
        resp = Value::object();
        resp.set("ok", Value(true));
        resp.set("max_nodes",
                 Value(static_cast<int64_t>(IncrementalTopo::kMaxNodes)));
        resp.set("max_edges",
                 Value(static_cast<int64_t>(IncrementalTopo::kMaxEdges)));
        return true;
    }

    resp = errResponse("unknown op: " + op);
    return true;
}

}  // namespace

int main() {
    std::ios::sync_with_stdio(false);
    std::cin.tie(nullptr);

    IncrementalTopo g;
    int64_t requests = 0;
    int64_t rejected = 0;

    std::string line;
    while (std::getline(std::cin, line)) {
        if (line.empty()) continue;
        Value resp;
        try {
            Value req = json::parse(line);
            bool cont = dispatch(g, req, resp, requests, rejected);
            std::cout << json::dump(resp) << '\n';
            std::cout.flush();
            if (!cont) break;
        } catch (const json::ParseError& e) {
            std::cout << json::dump(errResponse(std::string("invalid JSON: ") + e.what()))
                      << '\n';
            std::cout.flush();
        } catch (const std::exception& e) {
            std::cout << json::dump(errResponse(std::string("internal error: ") + e.what()))
                      << '\n';
            std::cout.flush();
        }
    }
    return 0;
}
