// main.cpp — joint-trajectory time parameterization HTTP service.
#include <nlohmann/json.hpp>

#include <chrono>
#include <csignal>
#include <cstdlib>
#include <iostream>
#include <string>
#include <thread>

#include "crypto.hpp"
#include "http_server.hpp"
#include "io_json.hpp"
#include "trajectory_time_parameterization.hpp"

namespace {
volatile std::sig_atomic_t g_shutdown = 0;
void OnSignal(int) { g_shutdown = 1; }

std::string PathOnly(const std::string& target) {
  const auto q = target.find('?');
  return q == std::string::npos ? target : target.substr(0, q);
}
}  // namespace

int main(int argc, char** argv) {
  std::string host = "127.0.0.1";
  int port = 8080;
  for (int i = 1; i < argc; ++i) {
    const std::string arg = argv[i];
    if ((arg == "--host" || arg == "-h") && i + 1 < argc) {
      host = argv[++i];
    } else if ((arg == "--port" || arg == "-p") && i + 1 < argc) {
      port = std::atoi(argv[++i]);
    } else if (arg == "--help") {
      std::cout << "Usage: jtp_server [--host 127.0.0.1] [--port 8080]\n";
      return 0;
    }
  }

  jtp::HttpServer server(
      host, port,
      [](const jtp::HttpRequest& req) -> jtp::HttpResponse {
        jtp::HttpResponse resp;
        const std::string path = PathOnly(req.target);

        if (req.method == "GET" && (path == "/health" || path == "/")) {
          resp.status = 200;
          resp.body =
              "{\"ok\":true,\"service\":"
              "\"joint-trajectory-time-parameterization\",\"version\":\"1.0.0\"}";
          return resp;
        }

        if (path != "/parameterize") {
          resp.status = 404;
          resp.body = "{\"ok\":false,\"error\":{\"code\":\"NOT_FOUND\","
                      "\"message\":\"unknown route; POST /parameterize\"}}";
          return resp;
        }
        if (req.method != "POST") {
          resp.status = 405;
          resp.extra_header = "Allow: POST\r\n";
          resp.body = "{\"ok\":false,\"error\":{\"code\":\"METHOD_NOT_ALLOWED\","
                      "\"message\":\"use POST /parameterize\"}}";
          return resp;
        }

        // Real cryptographic digest of the exact bytes received.
        const std::string digest = jtp::Sha256Hex(req.body);

        try {
          const jtp::ParameterizeRequest pr = jtp::ParseRequestJson(req.body);
          const jtp::ParameterizeResult result = jtp::Parameterize(pr);
          const nlohmann::json body = jtp::ResultToJson(result, digest);
          resp.status = 200;
          resp.body = body.dump();
        } catch (const jtp::ParameterizeException& e) {
          const nlohmann::json body = jtp::ErrorToJson(e.error());
          resp.status = e.error().status;
          resp.body = body.dump();
        }
        return resp;
      });

  try {
    server.Start();
  } catch (const std::exception& e) {
    std::cerr << "failed to start server: " << e.what() << "\n";
    return 1;
  }

  std::signal(SIGINT, OnSignal);
  std::signal(SIGTERM, OnSignal);
  std::cout << "joint trajectory time parameterization service listening on "
            << host << ":" << server.bound_port() << "\n"
            << "POST /parameterize  (JSON), GET /health\n";
  std::cout.flush();

  while (!g_shutdown) {
    std::this_thread::sleep_for(std::chrono::milliseconds(100));
  }
  std::cerr << "shutting down...\n";
  server.Stop();
  return 0;
}
