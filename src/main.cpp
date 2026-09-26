// HTTP/1.1 JSON backend for dominance-based fault location.
// Zero third-party dependencies: POSIX sockets + the hand-written JSON in json.hpp.
//
// Endpoints:
//   GET  /healthz                 -> service status
//   POST /api/analyze             -> reachability, idom tree, dominance frontiers
//   POST /api/dominates           -> point dominance queries
//
// Each POST is stateless: the graph travels in every request body.

#include "json.hpp"
#include "graph.hpp"
#include "dominance.hpp"

#include <arpa/inet.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <unistd.h>

#include <csignal>
#include <cstring>
#include <cstdio>
#include <cctype>
#include <cerrno>
#include <string>
#include <vector>
#include <sstream>
#include <iostream>
#include <atomic>
#include <chrono>
#include <algorithm>
#include <unordered_map>

namespace {

constexpr long MAX_BODY_BYTES = 16L * 1024 * 1024;
constexpr long long DEFAULT_PATH_CAP = 200000;

std::atomic<bool> g_shutdown{false};

void handleSignal(int) { g_shutdown.store(true); }

void installShutdownHandler(int sig) {
    struct sigaction sa {};
    sa.sa_handler = handleSignal;
    sigemptyset(&sa.sa_mask);
    sa.sa_flags = 0; // deliberately NO SA_RESTART: accept() must return EINTR
    sigaction(sig, &sa, nullptr);
}

bool recvSome(int fd, char* buf, size_t len, ssize_t& k) {
    do {
        k = ::recv(fd, buf, len, 0);
    } while (k < 0 && errno == EINTR);
    return k >= 0;
}

// ---------------------------------------------------------------- JSON helpers

json::Value jstr(const std::string& s) { return json::Value::makeString(s); }
json::Value jint(long long v) { return json::Value::makeInt(v); }
json::Value jbool(bool v) { return json::Value::makeBool(v); }

json::Value strArray(const std::vector<std::string>& xs) {
    json::Value a = json::Value::makeArray();
    for (const auto& x : xs) a.arr.push_back(jstr(x));
    return a;
}

std::string extractEdgeLabel(const json::Value& e, const std::string& which) {
    if (e.type == json::Value::Array && e.arr.size() == 2 &&
        e.arr[0].type == json::Value::String && e.arr[1].type == json::Value::String)
        return which == "from" ? e.arr[0].str : e.arr[1].str;
    if (e.type == json::Value::Object) {
        const json::Value* v = e.find(which);
        if (v && v->type == json::Value::String) return v->str;
    }
    return "";
}

// Parses the common graph payload. Throws std::runtime_error on any problem.
struct ParsedGraph {
    dom::Graph graph;
    bool wantDomSets = false;
    bool wantReferences = false;
    long long pathCap = DEFAULT_PATH_CAP;
};

ParsedGraph parseGraphPayload(const json::Value& root) {
    ParsedGraph out;
    if (root.type != json::Value::Object) throw std::runtime_error("request body must be a JSON object");
    const json::Value* jnodes = root.find("nodes");
    const json::Value* jentry = root.find("entry");
    if (!jnodes || jnodes->type != json::Value::Array) throw std::runtime_error("field 'nodes' must be an array of strings");
    if (!jentry || jentry->type != json::Value::String) throw std::runtime_error("field 'entry' must be a string");

    std::vector<std::string> labels;
    labels.reserve(jnodes->arr.size());
    for (const auto& n : jnodes->arr) {
        if (n.type != json::Value::String) throw std::runtime_error("every node must be a string");
        labels.push_back(n.str);
    }

    std::vector<std::pair<std::string, std::string>> edges;
    if (const json::Value* je = root.find("edges")) {
        if (je->type != json::Value::Array) throw std::runtime_error("field 'edges' must be an array");
        edges.reserve(je->arr.size());
        for (const auto& e : je->arr) {
            std::string a = extractEdgeLabel(e, "from");
            std::string b = extractEdgeLabel(e, "to");
            if (a.empty() || b.empty())
                throw std::runtime_error("each edge must be [from, to] or {\"from\":..,\"to\":..}");
            edges.emplace_back(a, b);
        }
    }

    out.graph = dom::Graph::build(labels, edges, jentry->str);

    if (const json::Value* opts = root.find("include")) {
        if (opts->type != json::Value::Object) throw std::runtime_error("field 'include' must be an object");
        if (const auto* v = opts->find("dominators")) out.wantDomSets = v && v->type == json::Value::Bool && v->boolean;
        if (const auto* v = opts->find("references")) out.wantReferences = v && v->type == json::Value::Bool && v->boolean;
    }
    if (const json::Value* v = root.find("pathCap")) {
        if (v->type == json::Value::Number) out.pathCap = std::max(1LL, (long long)v->number);
    }
    return out;
}

json::Value labelArray(const dom::Graph& g, const std::vector<int>& ids) {
    json::Value a = json::Value::makeArray();
    for (int v : ids) a.arr.push_back(jstr(g.labels[v]));
    return a;
}

json::Value errorResponse(const std::string& msg) {
    json::Value r = json::Value::makeObject();
    r.set("ok", jbool(false));
    r.set("error", jstr(msg));
    return r;
}

// ---------------------------------------------------------------- /api/analyze

json::Value handleAnalyze(const json::Value& root) {
    ParsedGraph pg = parseGraphPayload(root);
    const dom::Graph& g = pg.graph;

    dom::LTResult lt = dom::lengauerTarjan(g);

    // idom-tree depth for reachable nodes (entry = 0).
    std::vector<int> depth(g.n(), -1);
    depth[g.entry] = 0;
    // Walk in DFS preorder, which is parent-before-child.
    for (int v : lt.preorder) {
        if (v == g.entry) continue;
        depth[v] = depth[lt.idom[v]] + 1;
    }

    std::vector<std::string> reachLabels, unreachLabels;
    json::Value nodes = json::Value::makeArray();
    for (int v = 0; v < g.n(); ++v) {
        json::Value nv = json::Value::makeObject();
        nv.set("label", jstr(g.labels[v]));
        nv.set("reachable", jbool(lt.reachable[v]));
        if (lt.reachable[v]) {
            reachLabels.push_back(g.labels[v]);
            nv.set("idom", v == g.entry ? json::Value::makeNull() : jstr(g.labels[lt.idom[v]]));
            nv.set("idomTreeDepth", jint(depth[v]));
            nv.set("dominanceFrontier", labelArray(g, lt.df[v]));
            if (pg.wantDomSets) {
                // Derive full dominator set by walking idom chain.
                std::vector<int> ds;
                for (int x = v;; x = lt.idom[x]) {
                    ds.push_back(x);
                    if (x == lt.idom[x]) break;
                }
                std::sort(ds.begin(), ds.end());
                nv.set("dominators", labelArray(g, ds));
            }
        } else {
            unreachLabels.push_back(g.labels[v]);
            nv.set("idom", json::Value::makeNull());
            nv.set("idomTreeDepth", jint(-1));
            nv.set("dominanceFrontier", json::Value::makeArray());
            if (pg.wantDomSets) nv.set("dominators", json::Value::makeArray());
        }
        nodes.arr.push_back(std::move(nv));
    }

    json::Value treeEdges = json::Value::makeArray();
    for (int v : lt.preorder) {
        if (v == g.entry) continue;
        json::Value e = json::Value::makeObject();
        e.set("parent", jstr(g.labels[lt.idom[v]]));
        e.set("child", jstr(g.labels[v]));
        treeEdges.arr.push_back(std::move(e));
    }

    int edgeCount = 0;
    for (const auto& row : g.succ) edgeCount += (int)row.size();

    json::Value resp = json::Value::makeObject();
    resp.set("ok", jbool(true));
    resp.set("algorithm", jstr("lengauer-tarjan (semi-dominators + path compression); "
                                "dominance frontier via Cooper-Harvey-Kennedy idom-tree walk"));
    json::Value meta = json::Value::makeObject();
    meta.set("nodeCount", jint(g.n()));
    meta.set("edgeCount", jint(edgeCount));
    meta.set("entry", jstr(g.labels[g.entry]));
    meta.set("reachableCount", jint(lt.reachableCount));
    meta.set("unreachableCount", jint(g.n() - lt.reachableCount));
    resp.set("graph", meta);
    resp.set("reachable", strArray(reachLabels));
    resp.set("unreachable", strArray(unreachLabels));
    resp.set("nodes", nodes);
    resp.set("idomTreeEdges", treeEdges);

    if (pg.wantReferences) {
        json::Value ref = json::Value::makeObject();
        dom::NaiveResult nv = dom::naiveIterative(g);
        ref.set("naiveIterativeRounds", jint(nv.iterations));
        ref.set("idomAgreesWithNaive", jbool(dom::sameIdom(lt.idom, nv.idom)));
        ref.set("frontierAgreesWithNaive", jbool(dom::sameFrontiers(lt.df, nv.df)));

        // LT dominator sets, materialized from the idom chains.
        std::vector<std::vector<int>> ltSets(g.n());
        for (int v : lt.preorder) {
            for (int x = v;; x = lt.idom[x]) {
                ltSets[v].push_back(x);
                if (x == lt.idom[x]) break;
            }
            std::sort(ltSets[v].begin(), ltSets[v].end());
        }
        ref.set("domSetsAgreeWithNaive", jbool(dom::sameDomSets(ltSets, nv.domSets)));

        try {
            dom::PathEnumResult pe = dom::pathEnumeration(g, pg.pathCap);
            ref.set("simplePathEnumeration", jbool(true));
            ref.set("totalSimplePathsFromEntry", jint(pe.totalPaths));
            ref.set("domSetsAgreeWithPathEnum", jbool(dom::sameDomSets(nv.domSets, pe.domSets)));
        } catch (const std::exception& e) {
            ref.set("simplePathEnumeration", jbool(false));
            ref.set("pathEnumNote", jstr(e.what()));
            ref.set("domSetsAgreeWithPathEnum", jbool(false));
        }
        resp.set("references", ref);
    }
    return resp;
}

// -------------------------------------------------------------- /api/dominates

json::Value handleDominates(const json::Value& root) {
    ParsedGraph pg = parseGraphPayload(root);
    const json::Value* q = root.find("queries");
    if (!q || q->type != json::Value::Array) throw std::runtime_error("field 'queries' must be an array");

    dom::LTResult lt = dom::lengauerTarjan(pg.graph);
    const dom::Graph& g = pg.graph;

    std::unordered_map<std::string, int> id;
    for (int i = 0; i < g.n(); ++i) id.emplace(g.labels[i], i);

    json::Value results = json::Value::makeArray();
    for (const auto& item : q->arr) {
        if (item.type != json::Value::Object) throw std::runtime_error("each query must be an object {a,b}");
        const json::Value* ja = item.find("a");
        const json::Value* jb = item.find("b");
        if (!ja || ja->type != json::Value::String || !jb || jb->type != json::Value::String)
            throw std::runtime_error("each query needs string fields 'a' and 'b'");
        auto ia = id.find(ja->str);
        auto ib = id.find(jb->str);
        json::Value r = json::Value::makeObject();
        r.set("a", jstr(ja->str));
        r.set("b", jstr(jb->str));
        if (ia == id.end() || ib == id.end()) {
            r.set("known", jbool(false));
            r.set("dominates", jbool(false));
            r.set("properlyDominates", jbool(false));
        } else {
            int a = ia->second, b = ib->second;
            bool bothReach = lt.reachable[a] && lt.reachable[b];
            r.set("known", jbool(true));
            r.set("reachableA", jbool(lt.reachable[a]));
            r.set("reachableB", jbool(lt.reachable[b]));
            r.set("dominates", jbool(bothReach && dom::dominates(lt, a, b)));
            r.set("properlyDominates", jbool(bothReach && dom::properlyDominates(lt, a, b)));
        }
        results.arr.push_back(std::move(r));
    }
    json::Value resp = json::Value::makeObject();
    resp.set("ok", jbool(true));
    resp.set("results", results);
    return resp;
}

// --------------------------------------------------------------------- HTTP

struct HttpRequest {
    std::string method;
    std::string target;
    std::string body;
};

bool recvRequest(int fd, HttpRequest& req, std::string& err) {
    std::string raw;
    char buf[4096];
    size_t headerEnd = std::string::npos;
    while (headerEnd == std::string::npos) {
        ssize_t k;
        if (!recvSome(fd, buf, sizeof(buf), k)) { err = "read error"; return false; }
        if (k == 0) { err = ""; return false; } // connection closed
        raw.append(buf, (size_t)k);
        headerEnd = raw.find("\r\n\r\n");
        if (raw.size() > MAX_BODY_BYTES) { err = "headers/body too large"; return false; }
    }

    std::string headerBlock = raw.substr(0, headerEnd);
    std::istringstream hs(headerBlock);
    std::string requestLine;
    if (!std::getline(hs, requestLine)) { err = "malformed request line"; return false; }
    if (!requestLine.empty() && requestLine.back() == '\r') requestLine.pop_back();
    {
        std::istringstream rl(requestLine);
        std::string version;
        rl >> req.method >> req.target >> version;
        if (req.method.empty() || req.target.empty()) { err = "malformed request line"; return false; }
    }

    long contentLength = 0;
    std::string line;
    while (std::getline(hs, line)) {
        if (!line.empty() && line.back() == '\r') line.pop_back();
        auto colon = line.find(':');
        if (colon == std::string::npos) continue;
        std::string name = line.substr(0, colon);
        std::string value = line.substr(colon + 1);
        if (!value.empty() && value.front() == ' ') value.erase(value.begin());
        std::transform(name.begin(), name.end(), name.begin(),
                       [](unsigned char c) { return (char)std::tolower(c); });
        if (name == "content-length") {
            try { contentLength = std::stol(value); }
            catch (...) { err = "invalid Content-Length"; return false; }
        }
    }
    if (contentLength < 0 || contentLength > MAX_BODY_BYTES) { err = "body too large"; return false; }

    req.body = raw.substr(headerEnd + 4);
    while ((long)req.body.size() < contentLength) {
        ssize_t k;
        if (!recvSome(fd, buf, std::min(sizeof(buf), (size_t)(contentLength - req.body.size())), k)
            || k <= 0) { err = "truncated body"; return false; }
        req.body.append(buf, (size_t)k);
    }
    return true;
}

void sendJson(int fd, int status, const std::string& reason, const json::Value& payload) {
    std::string body = payload.dump(2);
    std::ostringstream oss;
    oss << "HTTP/1.1 " << status << ' ' << reason << "\r\n"
        << "Content-Type: application/json; charset=utf-8\r\n"
        << "Content-Length: " << body.size() << "\r\n"
        << "Connection: close\r\n"
        << "X-Content-Type-Options: nosniff\r\n"
        << "\r\n";
    std::string head = oss.str();
    size_t sent = 0;
    while (sent < head.size()) {
        ssize_t k = ::send(fd, head.data() + sent, head.size() - sent, MSG_NOSIGNAL);
        if (k <= 0) return;
        sent += (size_t)k;
    }
    sent = 0;
    while (sent < body.size()) {
        ssize_t k = ::send(fd, body.data() + sent, body.size() - sent, MSG_NOSIGNAL);
        if (k <= 0) return;
        sent += (size_t)k;
    }
}

void dispatch(int fd, const HttpRequest& req) {
    if (req.method == "GET" && (req.target == "/healthz" || req.target == "/health")) {
        json::Value r = json::Value::makeObject();
        r.set("ok", jbool(true));
        r.set("service", jstr("dominance-fault-location"));
        sendJson(fd, 200, "OK", r);
        return;
    }
    // Strip any query string, then match the route BEFORE checking the method:
    // an unknown path is 404 even when reached with a non-POST verb.
    std::string path = req.target.substr(0, req.target.find('?'));
    if (path != "/api/analyze" && path != "/api/dominates") {
        sendJson(fd, 404, "Not Found", errorResponse("unknown endpoint: " + path));
        return;
    }
    if (req.method != "POST") {
        sendJson(fd, 405, "Method Not Allowed", errorResponse("only POST is accepted for API endpoints"));
        return;
    }

    std::string parseErr;
    json::Value root = json::parse(req.body, parseErr);
    if (!parseErr.empty()) {
        sendJson(fd, 400, "Bad Request", errorResponse("invalid JSON: " + parseErr));
        return;
    }
    try {
        json::Value out = (path == "/api/analyze") ? handleAnalyze(root) : handleDominates(root);
        sendJson(fd, 200, "OK", out);
    } catch (const std::exception& e) {
        sendJson(fd, 422, "Unprocessable Entity", errorResponse(e.what()));
    }
}

} // namespace

int main(int argc, char** argv) {
    int port = 8080;
    for (int i = 1; i < argc; ++i) {
        std::string a = argv[i];
        if ((a == "--port" || a == "-p") && i + 1 < argc) port = std::stoi(argv[++i]);
        else {
            std::cerr << "usage: dom-server [--port N] (default 8080)\n";
            return 2;
        }
    }

    installShutdownHandler(SIGINT);
    installShutdownHandler(SIGTERM);
    std::signal(SIGPIPE, SIG_IGN);

    int srv = ::socket(AF_INET, SOCK_STREAM, 0);
    if (srv < 0) { std::perror("socket"); return 1; }
    int yes = 1;
    setsockopt(srv, SOL_SOCKET, SO_REUSEADDR, &yes, sizeof(yes));

    sockaddr_in addr{};
    addr.sin_family = AF_INET;
    addr.sin_addr.s_addr = htonl(INADDR_ANY);
    addr.sin_port = htons((uint16_t)port);
    if (bind(srv, (sockaddr*)&addr, sizeof(addr)) < 0) { std::perror("bind"); return 1; }
    if (listen(srv, 16) < 0) { std::perror("listen"); return 1; }

    std::cout << "dominance-fault-location listening on 0.0.0.0:" << port << std::endl;

    while (!g_shutdown.load()) {
        sockaddr_in cli{};
        socklen_t clen = sizeof(cli);
        int fd = accept(srv, (sockaddr*)&cli, &clen);
        if (fd < 0) {
            if (g_shutdown.load()) break; // SIGTERM/SIGINT interrupted accept
            if (errno == EINTR) continue;
            std::perror("accept");
            continue;
        }
        HttpRequest req;
        std::string err;
        if (recvRequest(fd, req, err)) {
            dispatch(fd, req);
        } else if (!err.empty()) {
            sendJson(fd, 400, "Bad Request", errorResponse(err));
        }
        ::close(fd);
    }
    ::close(srv);
    return 0;
}
