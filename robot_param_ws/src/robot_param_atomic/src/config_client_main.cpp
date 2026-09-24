// SPDX-License-Identifier: Apache-2.0
/// @file config_client_main.cpp
/// @brief Command-line client for the atomic configuration services.
///
/// Subcommands:
///   get
///   set   --expected V --rate R --cache C --latency L   (full snapshot)
///   patch --expected V [--rate R] [--cache C] [--latency L]
///         (missing fields are sent as the KEEP sentinel -1)
///   race  --expected V --threads N --rate R --cache C --latency L
///         (concurrent identical CAS commits; at most one may win)
///
/// Exit codes: 0 accepted / 1 rejected by the server / 2 transport or usage
/// error. Output is one JSON object on stdout so shell acceptance scripts can
/// parse it.
#include <chrono>
#include <cstdlib>
#include <iostream>
#include <map>
#include <memory>
#include <mutex>
#include <sstream>
#include <string>
#include <thread>
#include <vector>

#include <rclcpp/rclcpp.hpp>

#include "robot_param_atomic/srv/get_config.hpp"
#include "robot_param_atomic/srv/update_config.hpp"

namespace
{

using namespace std::chrono_literals;
using robot_param_atomic::srv::GetConfig;
using robot_param_atomic::srv::UpdateConfig;

constexpr double KEEP = -1.0;

struct Options
{
  // Node namespace the server runs in. The service names are always
  // "<ns>/robot_config/{update,get}". Default is the root namespace, where the
  // node is normally launched.
  std::string service_namespace = "";
  double expected = -999.0;
  double rate = KEEP;
  double cache = KEEP;
  double latency = KEEP;
  int threads = 4;
  std::chrono::seconds timeout = 10s;
};

// One lightweight node + client pair. Each racing thread owns its own so the
// concurrent test exercises independent rclcpp client objects, as N external
// processes would.
class ConfigClient
{
public:
  ConfigClient(const std::string & ns, const std::string & name)
  : node_(std::make_shared<rclcpp::Node>(name))
  {
    update_cli_ = node_->create_client<UpdateConfig>(ns + "/robot_config/update");
    get_cli_ = node_->create_client<GetConfig>(ns + "/robot_config/get");
  }

  bool wait_update(std::chrono::seconds timeout)
  {
    return update_cli_->wait_for_service(timeout);
  }

  bool wait_get(std::chrono::seconds timeout)
  {
    return get_cli_->wait_for_service(timeout);
  }

  std::shared_ptr<UpdateConfig::Response> update(
    double expected, double rate, double cache, double latency,
    std::chrono::seconds timeout, std::string & error)
  {
    auto req = std::make_shared<UpdateConfig::Request>();
    req->expected_version = expected;
    req->sampling_rate_hz = rate;
    req->cache_length_s = cache;
    req->allowed_latency_s = latency;
    auto fut = update_cli_->async_send_request(req);
    if (spin_until(fut, timeout) != rclcpp::FutureReturnCode::SUCCESS) {
      error = "update call timed out or failed";
      return nullptr;
    }
    return fut.get();
  }

  std::shared_ptr<GetConfig::Response> get(
    std::chrono::seconds timeout, std::string & error)
  {
    auto req = std::make_shared<GetConfig::Request>();
    auto fut = get_cli_->async_send_request(req);
    if (spin_until(fut, timeout) != rclcpp::FutureReturnCode::SUCCESS) {
      error = "get call timed out or failed";
      return nullptr;
    }
    return fut.get();
  }

  rclcpp::Node::SharedPtr node() { return node_; }

private:
  template <typename FutureT>
  rclcpp::FutureReturnCode spin_until(FutureT & fut, std::chrono::seconds t)
  {
    return rclcpp::spin_until_future_complete(node_, fut, t);
  }

  rclcpp::Node::SharedPtr node_;
  rclcpp::Client<UpdateConfig>::SharedPtr update_cli_;
  rclcpp::Client<GetConfig>::SharedPtr get_cli_;
};

std::string json_escape(const std::string & s)
{
  std::string out;
  for (char c : s) {
    if (c == '"' || c == '\\') { out.push_back('\\'); }
    out.push_back(c);
  }
  return out;
}

void print_update(const UpdateConfig::Response & r)
{
  std::ostringstream os;
  os << "{\"ok\":" << (r.ok ? "true" : "false")
     << ",\"code\":" << static_cast<int>(r.code)
     << ",\"message\":\"" << json_escape(r.message) << "\""
     << ",\"version\":" << r.version
     << ",\"sampling_rate_hz\":" << r.sampling_rate_hz
     << ",\"cache_length_s\":" << r.cache_length_s
     << ",\"allowed_latency_s\":" << r.allowed_latency_s << "}";
  std::cout << os.str() << std::endl;
}

void print_get(const GetConfig::Response & r)
{
  std::ostringstream os;
  os << "{\"version\":" << r.version
     << ",\"sampling_rate_hz\":" << r.sampling_rate_hz
     << ",\"cache_length_s\":" << r.cache_length_s
     << ",\"allowed_latency_s\":" << r.allowed_latency_s
     << ",\"config_hash\":\"" << json_escape(r.config_hash) << "\""
     << ",\"commit_seq\":" << r.commit_seq << "}";
  std::cout << os.str() << std::endl;
}

bool parse_double(const std::string & s, double & out)
{
  try {
    size_t used = 0;
    out = std::stod(s, &used);
    return used == s.size();
  } catch (...) {
    return false;
  }
}

// ---- subcommands -----------------------------------------------------------

int cmd_get(const Options & o)
{
  ConfigClient c(o.service_namespace, "cfg_client_get");
  if (!c.wait_get(o.timeout)) {
    std::cerr << "service not available" << std::endl;
    return 2;
  }
  std::string err;
  auto r = c.get(o.timeout, err);
  if (!r) {
    std::cerr << err << std::endl;
    return 2;
  }
  print_get(*r);
  return 0;
}

int cmd_write(const Options & o, bool partial)
{
  if (o.expected < -1.0) {
    std::cerr << "missing --expected <version>" << std::endl;
    return 2;
  }
  ConfigClient c(o.service_namespace, partial ? "cfg_client_patch" : "cfg_client_set");
  if (!c.wait_update(o.timeout)) {
    std::cerr << "service not available" << std::endl;
    return 2;
  }
  std::string err;
  auto r = c.update(o.expected, o.rate, o.cache, o.latency, o.timeout, err);
  if (!r) {
    std::cerr << err << std::endl;
    return 2;
  }
  print_update(*r);
  return r->ok ? 0 : 1;
}

// Fire N concurrent identical CAS commits from independent nodes. The atomic
// contract guarantees exactly one OK and N-1 REJECT_STALE_VERSION.
int cmd_race(const Options & o)
{
  if (o.expected < -1.0) {
    std::cerr << "missing --expected <version>" << std::endl;
    return 2;
  }
  struct Outcome
  {
    int thread = 0;
    bool ok = false;
    uint8_t code = 255;
    double version = -1;
    std::string message;
  };
  std::mutex m;
  std::vector<Outcome> results;
  results.reserve(o.threads);

  auto worker = [&](int id) {
    std::string name = "cfg_race_" + std::to_string(id);
    ConfigClient c(o.service_namespace, name);
    Outcome out;
    out.thread = id;
    if (c.wait_update(o.timeout)) {
      std::string err;
      auto r = c.update(o.expected, o.rate, o.cache, o.latency, o.timeout, err);
      if (r) {
        out.ok = r->ok;
        out.code = r->code;
        out.version = r->version;
        out.message = r->message;
      } else {
        out.message = err;
      }
    } else {
      out.message = "service unavailable";
    }
    std::lock_guard<std::mutex> lk(m);
    results.push_back(out);
  };

  std::vector<std::thread> pool;
  for (int i = 0; i < o.threads; ++i) {
    pool.emplace_back(worker, i);
  }
  for (auto & t : pool) {
    t.join();
  }

  int wins = 0;
  int stale = 0;
  for (const auto & r : results) {
    if (r.ok) { ++wins; }
    if (r.code == UpdateConfig::Response::REJECT_STALE_VERSION) { ++stale; }
  }

  std::ostringstream os;
  os << "{\"threads\":" << o.threads << ",\"wins\":" << wins
     << ",\"stale_rejections\":" << stale << ",\"results\":[";
  for (size_t i = 0; i < results.size(); ++i) {
    const auto & r = results[i];
    os << (i ? "," : "")
       << "{\"thread\":" << r.thread << ",\"ok\":" << (r.ok ? "true" : "false")
       << ",\"code\":" << static_cast<int>(r.code)
       << ",\"version\":" << r.version << "}";
  }
  os << "]}";
  std::cout << os.str() << std::endl;

  // Atomicity assertion surfaced as the process exit code.
  return wins == 1 && stale == o.threads - 1 ? 0 : 1;
}

void usage(const char * prog)
{
  std::cerr
    << "usage:\n"
    << "  " << prog << " get [--ns /robot_config]\n"
    << "  " << prog << " set --expected V --rate R --cache C --latency L\n"
    << "  " << prog << " patch --expected V [--rate R] [--cache C] [--latency L]\n"
    << "  " << prog
    << " race --expected V --threads N --rate R --cache C --latency L\n"
    << "\nexit codes: 0 accepted, 1 rejected, 2 transport/usage error\n";
}

}  // namespace

int main(int argc, char ** argv)
{
  rclcpp::init(argc, argv);

  if (argc < 2) {
    usage(argv[0]);
    rclcpp::shutdown();
    return 2;
  }
  const std::string cmd = argv[1];

  Options o;
  auto need = [&](int & i) {
    if (i + 1 >= argc) {
      std::cerr << "option " << argv[i] << " needs a value" << std::endl;
      std::exit(2);
    }
    return argv[++i];
  };

  for (int i = 2; i < argc; ++i) {
    std::string flag = argv[i];
    if (flag == "--ns") {
      o.service_namespace = need(i);
    } else if (flag == "--expected") {
      if (!parse_double(need(i), o.expected)) { std::exit(2); }
    } else if (flag == "--rate") {
      if (!parse_double(need(i), o.rate)) { std::exit(2); }
    } else if (flag == "--cache") {
      if (!parse_double(need(i), o.cache)) { std::exit(2); }
    } else if (flag == "--latency") {
      if (!parse_double(need(i), o.latency)) { std::exit(2); }
    } else if (flag == "--threads") {
      o.threads = std::atoi(need(i));
    } else if (flag == "--timeout") {
      o.timeout = std::chrono::seconds(std::atoi(need(i)));
    } else {
      std::cerr << "unknown option: " << flag << std::endl;
      usage(argv[0]);
      rclcpp::shutdown();
      return 2;
    }
  }

  int rc;
  if (cmd == "get") {
    rc = cmd_get(o);
  } else if (cmd == "set") {
    rc = cmd_write(o, false);
  } else if (cmd == "patch") {
    rc = cmd_write(o, true);
  } else if (cmd == "race") {
    rc = cmd_race(o);
  } else {
    usage(argv[0]);
    rc = 2;
  }

  rclcpp::shutdown();
  return rc;
}
