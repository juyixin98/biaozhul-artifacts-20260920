#include "app.hpp"

#include <nlohmann/json.hpp>

#include <chrono>
#include <cstdlib>
#include <fstream>
#include <iostream>
#include <sstream>
#include <string>

namespace {

const char* kUsage =
    "usage: tf_server [--host 127.0.0.1] [--port 8080]\n"
    "                 [--hmac-key KEY | --hmac-key-file PATH | --no-auth]\n"
    "                 [--timestamp-tolerance SEC] [--seed FILE.json ...]\n"
    "\n"
    "Environment overrides: TF_HOST TF_PORT TF_HMAC_KEY TF_HMAC_KEY_FILE\n"
    "                       TF_NO_AUTH TF_TIMESTAMP_TOLERANCE\n";

std::string getEnv(const char* name, const std::string& dflt = "") {
  const char* v = std::getenv(name);
  return v ? std::string(v) : dflt;
}

bool loadSeed(tf::FrameTree& tree, const std::string& path) {
  std::ifstream f(path);
  if (!f) {
    std::cerr << "seed: cannot open " << path << "\n";
    return false;
  }
  nlohmann::json doc;
  try {
    std::stringstream ss;
    ss << f.rdbuf();
    doc = nlohmann::json::parse(ss.str());
  } catch (const std::exception& e) {
    std::cerr << "seed: " << path << ": " << e.what() << "\n";
    return false;
  }

  auto parse_xform = [](const nlohmann::json& j, tf::Transform& out,
                        std::string& err) -> bool {
    const auto& tr = j.at("translation");
    if (!tr.is_array() || tr.size() != 3) {
      err = "translation must be [x,y,z]";
      return false;
    }
    out.t = tf::Vector3(tr[0].get<double>(), tr[1].get<double>(),
                        tr[2].get<double>());
    const auto& q = j.at("rotation");
    tf::Quaternion qn;
    auto chk = tf::validateQuaternion(q.at("w").get<double>(),
                                      q.at("x").get<double>(),
                                      q.at("y").get<double>(),
                                      q.at("z").get<double>(), &qn);
    if (!chk.ok) {
      err = chk.error;
      return false;
    }
    out.q = qn;
    return true;
  };

  // {"edges":[{"parent","child","static":true,"transform":...},
  //           {"parent","child","samples":[{"stamp_us","transform"}...]}]}
  for (const auto& e : doc.value("edges", nlohmann::json::array())) {
    std::string parent = e.at("parent").get<std::string>();
    std::string child = e.at("child").get<std::string>();
    bool is_static = e.value("static", false);
    if (is_static) {
      tf::Transform x;
      std::string err;
      if (!parse_xform(e.at("transform"), x, err))
        throw std::runtime_error("seed " + path + " edge " + parent + "->" +
                                 child + ": " + err);
      tree.addStatic(parent, child, x);
    } else {
      std::vector<tf::Sample> batch;
      for (const auto& s : e.at("samples")) {
        tf::Sample sm;
        sm.stamp_us = s.at("stamp_us").get<int64_t>();
        std::string err;
        if (!parse_xform(s.at("transform"), sm.xform, err))
          throw std::runtime_error("seed " + path + " edge " + parent + "->" +
                                   child + ": " + err);
        batch.push_back(std::move(sm));
      }
      tree.addDynamicSamples(parent, child, batch);
    }
  }
  return true;
}

}  // namespace

int main(int argc, char** argv) {
  std::string host = getEnv("TF_HOST", "127.0.0.1");
  int port = std::atoi(getEnv("TF_PORT", "8080").c_str());
  std::string key = getEnv("TF_HMAC_KEY");
  std::string key_file = getEnv("TF_HMAC_KEY_FILE");
  bool no_auth = getEnv("TF_NO_AUTH") == "1";
  int64_t tol = std::atoll(getEnv("TF_TIMESTAMP_TOLERANCE", "30").c_str());
  std::vector<std::string> seeds;

  for (int i = 1; i < argc; ++i) {
    std::string a = argv[i];
    auto need_val = [&]() -> std::string {
      if (i + 1 >= argc)
        throw std::runtime_error("missing value for " + a);
      return argv[++i];
    };
    if (a == "--host") host = need_val();
    else if (a == "--port") port = std::atoi(need_val().c_str());
    else if (a == "--hmac-key") key = need_val();
    else if (a == "--hmac-key-file") key_file = need_val();
    else if (a == "--no-auth") no_auth = true;
    else if (a == "--timestamp-tolerance") tol = std::atoll(need_val().c_str());
    else if (a == "--seed") seeds.push_back(need_val());
    else if (a == "-h" || a == "--help") {
      std::cout << kUsage;
      return 0;
    } else {
      std::cerr << "unknown argument: " << a << "\n" << kUsage;
      return 2;
    }
  }

  if (!key_file.empty()) {
    std::ifstream f(key_file);
    if (!f) {
      std::cerr << "cannot read key file " << key_file << "\n";
      return 2;
    }
    std::getline(f, key);
    while (!key.empty() && (key.back() == '\n' || key.back() == '\r'))
      key.pop_back();
  }
  if (!no_auth && key.empty()) {
    std::cerr
        << "WARNING: no HMAC key set; starting UNAUTHENTICATED. Set "
           "--hmac-key/--hmac-key-file or --no-auth to silence this warning.\n";
    no_auth = true;
  }

  tf::App app(no_auth ? "" : key, tol);
  for (const auto& s : seeds)
    if (!loadSeed(app.tree(), s)) return 2;

  tf::HttpServer server(host, port,
                        [&app](const tf::HttpRequest& r) {
                          return app.dispatch(r);
                        });
  try {
    server.start();
  } catch (const std::exception& e) {
    std::cerr << "server start failed: " << e.what() << "\n";
    return 1;
  }
  std::cerr << "tf_server listening on http://" << host << ":" << server.port()
            << " auth=" << (no_auth ? "off" : "hmac-sha256") << "\n";
  server.waitForSignal();
  std::cerr << "tf_server stopped\n";
  return 0;
}
