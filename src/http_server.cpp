#include "http_server.hpp"

#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <signal.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <sys/types.h>
#include <unistd.h>

#include <algorithm>
#include <cctype>
#include <cerrno>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <stdexcept>
#include <string>

#include "json.hpp"
#include "protocol.hpp"

namespace http {

namespace {

volatile sig_atomic_t g_shouldStop = 0;

void handleSignal(int) { g_shouldStop = 1; }

bool writeAll(int fd, const std::string& data) {
  size_t sent = 0;
  while (sent < data.size()) {
    ssize_t n = ::write(fd, data.data() + sent, data.size() - sent);
    if (n < 0) {
      if (errno == EINTR) continue;
      return false;
    }
    sent += static_cast<size_t>(n);
  }
  return true;
}

std::string statusReason(int status) {
  switch (status) {
    case 200: return "OK";
    case 400: return "Bad Request";
    case 404: return "Not Found";
    case 405: return "Method Not Allowed";
    case 413: return "Payload Too Large";
    case 500: return "Internal Server Error";
    default: return "Status";
  }
}

void sendResponse(int client, int status, const std::string& body,
                  const std::string& contentType = "application/json") {
  std::string response;
  response += "HTTP/1.1 " + std::to_string(status) + " " +
              statusReason(status) + "\r\n";
  response += "Content-Type: " + contentType + "; charset=utf-8\r\n";
  response += "Content-Length: " + std::to_string(body.size()) + "\r\n";
  response += "Connection: close\r\n";
  response += "X-Content-Type-Options: nosniff\r\n";
  response += "\r\n";
  response += body;
  writeAll(client, response);
}

std::string jsonErrorBody(const std::string& code,
                          const std::string& message) {
  json::Value root = json::Value::makeObject();
  json::Value error = json::Value::makeObject();
  error.members.emplace_back("code", json::Value::makeString(code));
  error.members.emplace_back("message", json::Value::makeString(message));
  root.members.emplace_back("ok", json::Value::makeBoolean(false));
  root.members.emplace_back("error", error);
  return json::dump(root);
}

std::string toLower(std::string value) {
  std::transform(value.begin(), value.end(), value.begin(),
                 [](unsigned char c) { return std::tolower(c); });
  return value;
}

// Reads until CRLFCRLF. Returns the header block (without trailing blank
// line) and fills bodyBytesAlreadyRead in case bytes of the body arrived
// together with the headers.
bool readHeaders(int client, std::string& headerBlock,
                 std::string& bodyPrefix) {
  std::string buffer;
  char chunk[4096];
  while (buffer.find("\r\n\r\n") == std::string::npos) {
    ssize_t n = ::read(client, chunk, sizeof(chunk));
    if (n == 0) return false;  // peer closed
    if (n < 0) {
      if (errno == EINTR) continue;
      return false;
    }
    buffer.append(chunk, static_cast<size_t>(n));
    if (buffer.size() > 64 * 1024) return false;  // headers too large
  }
  size_t split = buffer.find("\r\n\r\n");
  headerBlock = buffer.substr(0, split);
  bodyPrefix = buffer.substr(split + 4);
  return true;
}

void handleClient(int client) {
  std::string headerBlock;
  std::string bodyPrefix;
  if (!readHeaders(client, headerBlock, bodyPrefix)) {
    return;
  }

  // Request line.
  size_t firstLineEnd = headerBlock.find("\r\n");
  std::string requestLine = headerBlock.substr(0, firstLineEnd);
  std::string method;
  std::string target;
  {
    size_t space1 = requestLine.find(' ');
    size_t space2 = requestLine.rfind(' ');
    if (space1 == std::string::npos || space1 == space2) {
      sendResponse(client, 400,
                   jsonErrorBody("BAD_REQUEST", "malformed request line"));
      return;
    }
    method = requestLine.substr(0, space1);
    target = requestLine.substr(space1 + 1, space2 - space1 - 1);
    size_t query = target.find('?');
    if (query != std::string::npos) target.resize(query);
  }

  if (method == "GET" && target == "/health") {
    json::Value body = json::Value::makeObject();
    body.members.emplace_back("status", json::Value::makeString("ok"));
    body.members.emplace_back("service",
                              json::Value::makeString("bipartite-matching"));
    sendResponse(client, 200, json::dump(body));
    return;
  }

  if (target != "/solve") {
    sendResponse(client, 404,
                 jsonErrorBody("NOT_FOUND",
                               "available endpoints: POST /solve, GET /health"));
    return;
  }
  if (method != "POST") {
    sendResponse(client, 405,
                 jsonErrorBody("METHOD_NOT_ALLOWED", "use POST /solve"));
    return;
  }

  size_t contentLength = 0;
  std::string lines = headerBlock.substr(firstLineEnd + 2);
  size_t pos = 0;
  while (pos < lines.size()) {
    size_t end = lines.find("\r\n", pos);
    if (end == std::string::npos) end = lines.size();
    std::string line = lines.substr(pos, end - pos);
    size_t colon = line.find(':');
    if (colon != std::string::npos) {
      std::string name = toLower(line.substr(0, colon));
      std::string value = line.substr(colon + 1);
      size_t first = value.find_first_not_of(" \t");
      size_t last = value.find_last_not_of(" \t");
      if (first != std::string::npos) value = value.substr(first, last - first + 1);
      if (name == "content-length") {
        if (value.empty() ||
            !std::all_of(value.begin(), value.end(),
                         [](char c) { return std::isdigit(static_cast<unsigned char>(c)); })) {
          sendResponse(client, 400,
                       jsonErrorBody("BAD_REQUEST",
                                     "invalid Content-Length header"));
          return;
        }
        errno = 0;
        try {
          contentLength = static_cast<size_t>(
              std::stoull(value, nullptr, 10));
        } catch (const std::exception&) {
          sendResponse(client, 400,
                       jsonErrorBody("BAD_REQUEST",
                                     "invalid Content-Length header"));
          return;
        }
      }
    }
    pos = end + 2;
  }

  if (contentLength == 0) {
    sendResponse(client, 400,
                 jsonErrorBody("BAD_REQUEST",
                               "POST /solve requires a JSON body"));
    return;
  }
  if (contentLength > kMaxBodyBytes) {
    sendResponse(client, 413,
                 jsonErrorBody("PAYLOAD_TOO_LARGE",
                               "request body exceeds the 16 MiB limit"));
    return;
  }

  std::string body = bodyPrefix;
  body.reserve(contentLength);
  char chunk[16384];
  while (body.size() < contentLength) {
    ssize_t n = ::read(client, chunk,
                       std::min(sizeof(chunk), contentLength - body.size()));
    if (n == 0) break;
    if (n < 0) {
      if (errno == EINTR) continue;
      sendResponse(client, 400,
                   jsonErrorBody("BAD_REQUEST", "incomplete request body"));
      return;
    }
    body.append(chunk, static_cast<size_t>(n));
  }
  if (body.size() != contentLength) {
    sendResponse(client, 400,
                 jsonErrorBody("BAD_REQUEST", "incomplete request body"));
    return;
  }

  json::Value request;
  try {
    request = json::parse(body);
  } catch (const json::ParseError& error) {
    sendResponse(client, 400,
                 jsonErrorBody("INVALID_JSON", error.what()));
    return;
  }

  json::Value response = protocol::handleRequest(request);
  int status = 200;
  if (const json::Value* ok = response.find("ok");
      ok == nullptr || !ok->boolean) {
    const json::Value* error = response.find("error");
    std::string code;
    if (error != nullptr) {
      if (const json::Value* codeValue = error->find("code");
          codeValue != nullptr && codeValue->isString()) {
        code = codeValue->text;
      }
    }
    if (code == "LIMIT_EXCEEDED" || code == "PAYLOAD_TOO_LARGE") {
      status = 413;
    } else {
      // INVALID_REQUEST, BRUTE_FORCE_TOO_LARGE and similar are bad input.
      status = 400;
    }
  }
  sendResponse(client, status, json::dump(response));
}

}  // namespace

int runServer(const ServerConfig& config) {
  struct sigaction action;
  std::memset(&action, 0, sizeof(action));
  action.sa_handler = handleSignal;
  sigaction(SIGINT, &action, nullptr);
  sigaction(SIGTERM, &action, nullptr);
  signal(SIGPIPE, SIG_IGN);

  int listener = ::socket(AF_INET, SOCK_STREAM, 0);
  if (listener < 0) {
    std::fprintf(stderr, "socket() failed: %s\n", std::strerror(errno));
    return 1;
  }
  int enabled = 1;
  setsockopt(listener, SOL_SOCKET, SO_REUSEADDR, &enabled, sizeof(enabled));

  struct sockaddr_in address;
  std::memset(&address, 0, sizeof(address));
  address.sin_family = AF_INET;
  address.sin_port = htons(static_cast<uint16_t>(config.port));
  if (::inet_pton(AF_INET, config.host.c_str(), &address.sin_addr) != 1) {
    std::fprintf(stderr, "invalid host address: %s\n", config.host.c_str());
    ::close(listener);
    return 1;
  }
  if (::bind(listener, reinterpret_cast<struct sockaddr*>(&address),
             sizeof(address)) != 0) {
    std::fprintf(stderr, "bind(%s:%d) failed: %s\n", config.host.c_str(),
                 config.port, std::strerror(errno));
    ::close(listener);
    return 1;
  }
  if (::listen(listener, 32) != 0) {
    std::fprintf(stderr, "listen() failed: %s\n", std::strerror(errno));
    ::close(listener);
    return 1;
  }
  std::fprintf(stderr,
               "bipartite-matching service listening on http://%s:%d "
               "(POST /solve)\n",
               config.host.c_str(), config.port);

  while (!g_shouldStop) {
    struct sockaddr_in clientAddress;
    socklen_t clientLength = sizeof(clientAddress);
    int client = ::accept(listener,
                          reinterpret_cast<struct sockaddr*>(&clientAddress),
                          &clientLength);
    if (client < 0) {
      if (errno == EINTR) continue;
      std::fprintf(stderr, "accept() failed: %s\n", std::strerror(errno));
      continue;
    }
    // Bound how long a client may hold a connection idle.
    struct timeval timeout;
    timeout.tv_sec = 10;
    timeout.tv_usec = 0;
    setsockopt(client, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout));
    handleClient(client);
    ::close(client);
  }

  std::fprintf(stderr, "shutting down\n");
  ::close(listener);
  return 0;
}

}  // namespace http
