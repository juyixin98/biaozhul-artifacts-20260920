#include "server.hpp"

#include <arpa/inet.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <unistd.h>

#include <cerrno>
#include <cstring>
#include <iostream>
#include <sstream>

#include "solver.hpp"

namespace sat {

namespace {

constexpr size_t MAX_BODY = 1 << 20;  // 1 MiB request body cap

std::string jsonError(const std::string& message) {
    minijson::Value v = minijson::Value::makeObj();
    v.set("error", minijson::Value::makeStr(message));
    return minijson::dump(v);
}

std::pair<int, std::string> handleSolve(const minijson::Value& req) {
    ParseResult pr = parseCnfFromJson(req);
    if (!pr.ok) return {400, jsonError(pr.error)};
    CNF cnf = std::move(pr.cnf);
    normalizeCnf(cnf);
    if (cnf.numVars > MAX_DPLL_VARS) {
        return {400, jsonError("num_vars exceeds DPLL limit of " +
                               std::to_string(MAX_DPLL_VARS))};
    }
    if (cnf.clauses.size() > MAX_CLAUSES) {
        return {400, jsonError("clause count exceeds limit of " +
                               std::to_string(MAX_CLAUSES))};
    }
    SolveOptions opts;
    if (const minijson::Value* nl = req.find("node_limit")) {
        if (nl->type != minijson::Value::INT || nl->i <= 0) {
            return {400, jsonError("node_limit must be a positive integer")};
        }
        opts.nodeLimit = static_cast<std::uint64_t>(nl->i);
    }
    SolveResult r = dpll(cnf, opts);
    minijson::Value out = resultToJson(r, cnf);
    return {200, minijson::dump(out)};
}

std::pair<int, std::string> handleBrute(const minijson::Value& req) {
    ParseResult pr = parseCnfFromJson(req);
    if (!pr.ok) return {400, jsonError(pr.error)};
    CNF cnf = std::move(pr.cnf);
    normalizeCnf(cnf);
    BruteResult br;
    std::string err;
    if (!bruteForce(cnf, br, err)) return {400, jsonError(err)};
    minijson::Value out = minijson::Value::makeObj();
    out.set("status", minijson::Value::makeStr(br.sat ? "sat" : "unsat"));
    out.set("num_vars", minijson::Value::makeInt(cnf.numVars));
    out.set("assignments_tested",
            minijson::Value::makeInt(static_cast<long long>(br.tested)));
    if (br.sat) {
        minijson::Value model = minijson::Value::makeArr();
        for (int v = 1; v <= cnf.numVars; ++v) {
            minijson::Value entry = minijson::Value::makeObj();
            entry.set("variable", minijson::Value::makeInt(v));
            entry.set("value", minijson::Value::makeBool(br.model[v] == 1));
            model.arr.push_back(std::move(entry));
        }
        out.set("model", std::move(model));
    } else {
        out.set("model", minijson::Value::makeNull());
    }
    return {200, minijson::dump(out)};
}

// jsonToResult lives in solver.cpp so the CLI verifier can share it.

std::pair<int, std::string> handleVerify(const minijson::Value& req) {
    ParseResult pr = parseCnfFromJson(req);
    if (!pr.ok) return {400, jsonError(pr.error)};
    CNF cnf = std::move(pr.cnf);
    normalizeCnf(cnf);
    SolveResult r;
    std::string err;
    if (!jsonToResult(req, r, err)) return {400, jsonError(err)};
    bool ok = verifyProof(cnf, r, err);
    minijson::Value out = minijson::Value::makeObj();
    out.set("valid", minijson::Value::makeBool(ok));
    if (!ok) out.set("reason", minijson::Value::makeStr(err));
    return {200, minijson::dump(out)};
}

}  // namespace

std::pair<int, std::string> handleRequest(const std::string& method,
                                          const std::string& path,
                                          const std::string& body) {
    if (method == "GET" && path == "/healthz") {
        return {200, "{\"status\":\"ok\"}"};
    }
    if (method != "POST") {
        return {405, jsonError("method not allowed; use POST (or GET /healthz)")};
    }
    if (path != "/solve" && path != "/brute" && path != "/verify") {
        return {404, jsonError("unknown endpoint; use /solve, /brute or /verify")};
    }
    minijson::Value req;
    std::string err;
    if (!minijson::parse(body, req, err)) {
        return {400, jsonError("invalid JSON: " + err)};
    }
    if (path == "/solve") return handleSolve(req);
    if (path == "/brute") return handleBrute(req);
    return handleVerify(req);
}

namespace {

bool readRequest(int fd, std::string& method, std::string& path, std::string& body) {
    std::string data;
    data.reserve(4096);
    char buf[4096];
    size_t headerEnd = std::string::npos;
    while (headerEnd == std::string::npos) {
        ssize_t n = ::recv(fd, buf, sizeof(buf), 0);
        if (n <= 0) return false;
        data.append(buf, static_cast<size_t>(n));
        if (data.size() > MAX_BODY) return false;
        headerEnd = data.find("\r\n\r\n");
    }
    std::string headers = data.substr(0, headerEnd);
    std::string rest = data.substr(headerEnd + 4);

    std::istringstream hs(headers);
    std::string requestLine;
    if (!std::getline(hs, requestLine)) return false;
    std::istringstream rl(requestLine);
    std::string version;
    if (!(rl >> method >> path >> version)) return false;

    long long contentLength = -1;
    std::string line;
    while (std::getline(hs, line)) {
        if (!line.empty() && line.back() == '\r') line.pop_back();
        std::string lower = line;
        for (char& c : lower) c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
        const std::string key = "content-length:";
        if (lower.compare(0, key.size(), key) == 0) {
            contentLength = std::stoll(lower.substr(key.size()));
        }
    }
    if (contentLength < 0) contentLength = 0;  // e.g. GET without a body
    if (static_cast<size_t>(contentLength) > MAX_BODY) return false;
    body = std::move(rest);
    while (body.size() < static_cast<size_t>(contentLength)) {
        ssize_t n = ::recv(fd, buf, sizeof(buf), 0);
        if (n <= 0) return false;
        body.append(buf, static_cast<size_t>(n));
    }
    body.resize(static_cast<size_t>(contentLength));
    return true;
}

void sendResponse(int fd, int status, const std::string& body) {
    const char* reason = status == 200 ? "OK"
                         : status == 400 ? "Bad Request"
                         : status == 404 ? "Not Found"
                         : status == 405 ? "Method Not Allowed" : "Error";
    std::ostringstream out;
    out << "HTTP/1.1 " << status << ' ' << reason << "\r\n"
        << "Content-Type: application/json\r\n"
        << "Content-Length: " << body.size() << "\r\n"
        << "Connection: close\r\n\r\n"
        << body;
    std::string text = out.str();
    size_t sent = 0;
    while (sent < text.size()) {
        ssize_t n = ::send(fd, text.data() + sent, text.size() - sent, 0);
        if (n <= 0) return;
        sent += static_cast<size_t>(n);
    }
}

}  // namespace

int runServer(int port) {
    int serverFd = ::socket(AF_INET, SOCK_STREAM, 0);
    if (serverFd < 0) {
        std::cerr << "socket() failed: " << std::strerror(errno) << "\n";
        return 1;
    }
    int opt = 1;
    ::setsockopt(serverFd, SOL_SOCKET, SO_REUSEADDR, &opt, sizeof(opt));

    sockaddr_in addr{};
    addr.sin_family = AF_INET;
    addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    addr.sin_port = htons(static_cast<uint16_t>(port));
    if (::bind(serverFd, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) < 0) {
        std::cerr << "bind() failed on 127.0.0.1:" << port << ": "
                  << std::strerror(errno) << "\n";
        ::close(serverFd);
        return 1;
    }
    if (::listen(serverFd, 16) < 0) {
        std::cerr << "listen() failed: " << std::strerror(errno) << "\n";
        ::close(serverFd);
        return 1;
    }
    std::cerr << "sat-solver listening on http://127.0.0.1:" << port
              << " (endpoints: POST /solve, /brute, /verify; GET /healthz)\n";

    while (true) {
        int client = ::accept(serverFd, nullptr, nullptr);
        if (client < 0) {
            if (errno == EINTR) continue;
            std::cerr << "accept() failed: " << std::strerror(errno) << "\n";
            break;
        }
        std::string method, path, body;
        if (readRequest(client, method, path, body)) {
            auto [status, responseBody] = handleRequest(method, path, body);
            sendResponse(client, status, responseBody);
        } else {
            sendResponse(client, 400, jsonError("malformed HTTP request"));
        }
        ::close(client);
    }
    ::close(serverFd);
    return 0;
}

}  // namespace sat
