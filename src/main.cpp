// JSON command-line interface for the incremental topological sorter.
//
// Protocol: newline-delimited JSON on stdin, one response per line on stdout.
// A request may also be a single object {"ops":[...]} executed in order.
//
//   {"op":"reset","n":<int>}                 create an empty graph (0..n-1)
//   {"op":"addEdge","u":<int>,"v":<int>}     insert u -> v
//   {"op":"order"}                           current topological order
//   {"op":"hasEdge","u":..,"v":..}
//   {"op":"stats"}                           vertex/edge counters
//   {"op":"validate"}                        full independent re-check (Kahn)
//   {"op":"quit"}                            end session
//
// Usage:
//   ./topo_srv < examples/request.jsonl     # file or stream of requests
//   echo '{"op":"reset","n":3}' | ./topo_srv
//
// No third-party libraries; exit code is non-zero only on fatal protocol
// errors. Per-command failures are reported in-band as {"ok":false,...}.
#include <iostream>
#include <memory>
#include <sstream>
#include <string>

#include "json.hpp"
#include "topo.hpp"

namespace {

using json::Value;
using topo::IncrementalTopo;
using topo::Graph;
using topo::kahnOrder;
using topo::validateOrder;

// Hard size limits (the task calls for a bounded, verifiable backend).
constexpr int kMaxN = 200000;

struct Session {
    std::unique_ptr<IncrementalTopo> topo;
    // Mirror of committed edges for the independent validate command.
    std::unique_ptr<Graph> ref;
};

Value ok() { Value r = Value::makeObj(); r.obj["ok"] = Value::makeBool(true); return r; }

Value fail(const std::string& code, const std::string& msg) {
    Value r = Value::makeObj();
    r.obj["ok"] = Value::makeBool(false);
    r.obj["error"] = Value::makeStr(code);
    r.obj["message"] = Value::makeStr(msg);
    return r;
}

Value intVecToJson(const std::vector<int>& xs) {
    Value a = Value::makeArr();
    for (int x : xs) a.arr.push_back(Value::makeInt(x));
    return a;
}

bool requireInt(const Value& req, const char* key, int& out) {
    const Value* v = req.find(key);
    if (!v || v->type != Value::Int) return false;
    long long x = v->integer;
    if (x < 0 || x > kMaxN) return false;
    out = static_cast<int>(x);
    return true;
}

Value handleReset(const Value& req, Session& s) {
    int n = -1;
    if (!requireInt(req, "n", n) || n < 0)
        return fail("invalid_request", "field 'n' must be an integer in [0, " +
                                        std::to_string(kMaxN) + "]");
    s.topo = std::make_unique<IncrementalTopo>(n);
    s.ref = std::make_unique<Graph>(n);
    Value r = ok();
    r.obj["n"] = Value::makeInt(n);
    return r;
}

Value handleAddEdge(const Value& req, Session& s) {
    if (!s.topo) return fail("no_graph", "send {\"op\":\"reset\",\"n\":...} first");
    int u = -1, v = -1;
    if (!requireInt(req, "u", u) || !requireInt(req, "v", v))
        return fail("invalid_request", "fields 'u' and 'v' must be vertex ids");
    int n = s.topo->numVertices();
    if (u >= n || v >= n)
        return fail("out_of_range", "vertex id exceeds n-1=" + std::to_string(n - 1));

    const long long revBefore = s.topo->revision();
    auto res = s.topo->insertEdge(u, v);

    Value r = Value::makeObj();
    r.obj["ok"] = Value::makeBool(true);
    r.obj["edge"] = Value::makeStr(std::to_string(u) + "->" + std::to_string(v));
    r.obj["accepted"] = Value::makeBool(res.accepted);
    r.obj["duplicate"] = Value::makeBool(res.duplicate);
    r.obj["cycle"] = Value::makeBool(res.cycle);
    r.obj["reordered"] = Value::makeBool(res.reordered);
    r.obj["visited"] = Value::makeInt(res.visited);
    if (res.cycle) {
        // Failed insertion: prove the graph did not change.
        r.obj["cyclePath"] = intVecToJson(res.cyclePath);
        r.obj["graphUnchanged"] = Value::makeBool(
            s.topo->revision() == revBefore && !s.topo->hasEdge(u, v));
    } else {
        s.ref->addEdge(u, v);
    }
    r.obj["edges"] = Value::makeInt(static_cast<long long>(s.topo->edgeCount()));
    return r;
}

Value handleOrder(const Session& s) {
    if (!s.topo) return fail("no_graph", "send {\"op\":\"reset\",\"n\":...} first");
    Value r = ok();
    r.obj["order"] = intVecToJson(s.topo->order());
    return r;
}

Value handleHasEdge(const Value& req, const Session& s) {
    if (!s.topo) return fail("no_graph", "send {\"op\":\"reset\",\"n\":...} first");
    int u = -1, v = -1;
    if (!requireInt(req, "u", u) || !requireInt(req, "v", v))
        return fail("invalid_request", "fields 'u' and 'v' must be vertex ids");
    Value r = ok();
    r.obj["hasEdge"] = Value::makeBool(s.topo->hasEdge(u, v));
    return r;
}

Value handleStats(const Session& s) {
    if (!s.topo) return fail("no_graph", "send {\"op\":\"reset\",\"n\":...} first");
    Value r = ok();
    r.obj["vertices"] = Value::makeInt(s.topo->numVertices());
    r.obj["edges"] = Value::makeInt(static_cast<long long>(s.topo->edgeCount()));
    return r;
}

Value handleValidate(const Session& s) {
    if (!s.topo) return fail("no_graph", "send {\"op\":\"reset\",\"n\":...} first");
    std::vector<int> kahn;
    bool acyclic = kahnOrder(*s.ref, kahn);
    std::string err = validateOrder(*s.ref, s.topo->order());
    Value r = ok();
    r.obj["referenceAcyclic"] = Value::makeBool(acyclic);
    r.obj["incrementalOrderValid"] = Value::makeBool(err.empty());
    if (!err.empty()) r.obj["violation"] = Value::makeStr(err);
    return r;
}

Value dispatch(const Value& req, Session& s) {
    const Value* opv = req.find("op");
    if (!opv || opv->type != Value::Str)
        return fail("invalid_request", "missing string field 'op'");
    const std::string& op = opv->str;
    if (op == "reset") return handleReset(req, s);
    if (op == "addEdge") return handleAddEdge(req, s);
    if (op == "order") return handleOrder(s);
    if (op == "hasEdge") return handleHasEdge(req, s);
    if (op == "stats") return handleStats(s);
    if (op == "validate") return handleValidate(s);
    if (op == "quit") return fail("invalid_request", "use stream EOF or op 'quit'");
    return fail("unknown_op", "unknown op: " + op);
}

bool respond(const std::string& line, Session& s, bool& quit) {
    if (line.find_first_not_of(" \t\r\n") == std::string::npos) return true;
    Value req;
    std::string err;
    if (!json::parse(line, req, err)) {
        std::cout << json::dump(fail("bad_json", err)) << '\n';
        return true; // keep the session alive
    }
    if (req.type == Value::Obj) {
        const Value* opv = req.find("op");
        if (opv && opv->type == Value::Str && opv->str == "quit") { quit = true; return true; }
        if (const Value* ops = req.find("ops")) {
            if (ops->type != Value::Arr) {
                std::cout << json::dump(fail("invalid_request", "'ops' must be an array")) << '\n';
                return true;
            }
            Value results = Value::makeArr();
            for (const Value& sub : ops->arr) {
                if (sub.type != Value::Obj) {
                    results.arr.push_back(fail("invalid_request", "each op must be an object"));
                    continue;
                }
                const Value* o = sub.find("op");
                if (o && o->type == Value::Str && o->str == "quit") { quit = true; break; }
                results.arr.push_back(dispatch(sub, s));
            }
            Value wrap = ok();
            wrap.obj["results"] = std::move(results);
            std::cout << json::dump(wrap) << '\n';
            return true;
        }
        std::cout << json::dump(dispatch(req, s)) << '\n';
        return true;
    }
    std::cout << json::dump(fail("invalid_request", "request must be a JSON object")) << '\n';
    return true;
}

} // namespace

int main() {
    std::ios::sync_with_stdio(false);
    std::cin.tie(nullptr);

    Session session;
    std::string line;
    bool quit = false;
    while (!quit && std::getline(std::cin, line)) {
        respond(line, session, quit);
        std::cout.flush();
    }
    return 0;
}
