// SPDX-License-Identifier: Apache-2.0
//
// Parameter client / CLI for robot_param_atomic.
//
//   robot_config_client get
//   robot_config_client update --expected-version N
//                              [--rate HZ] [--buffer SAMPLES] [--latency MS]
//
// Exit codes mirror service status (0 OK, 1 VALIDATION, 2 VERSION_CONFLICT,
// 3 PERSISTENCE_FAILED, 4 REENTRANT), 10 = transport/usage error.
#include <chrono>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <iomanip>
#include <iostream>
#include <sstream>
#include <string>

#include <rclcpp/rclcpp.hpp>

#include "robot_param_atomic/msg/config_snapshot.hpp"
#include "robot_param_atomic/srv/atomic_update.hpp"
#include "robot_param_atomic/srv/get_config.hpp"

namespace {

constexpr auto kWaitTimeout = std::chrono::seconds(10);

const char* status_name(std::uint8_t s) {
  switch (s) {
    case 0:
      return "OK";
    case 1:
      return "REJECTED_VALIDATION";
    case 2:
      return "VERSION_CONFLICT";
    case 3:
      return "PERSISTENCE_FAILED";
    case 4:
      return "REENTRANT_UPDATE";
    default:
      return "UNKNOWN";
  }
}

const char* load_state_name(std::uint8_t s) {
  switch (s) {
    case 0:
      return "FRESH";
    case 1:
      return "LOADED";
    case 2:
      return "RECOVERED";
    case 3:
      return "NO_VALID_CONFIG";
    default:
      return "UNKNOWN";
  }
}

bool parse_u64(const std::string& text, std::uint64_t& out) {
  if (text.empty()) {
    return false;
  }
  std::size_t consumed = 0;
  try {
    unsigned long long v = std::stoull(text, &consumed, 10);
    if (consumed != text.size()) {
      return false;
    }
    out = static_cast<std::uint64_t>(v);
  } catch (...) {
    return false;
  }
  return true;
}

bool parse_double(const std::string& text, double& out) {
  if (text.empty()) {
    return false;
  }
  std::size_t consumed = 0;
  try {
    double v = std::stod(text, &consumed);
    if (consumed != text.size()) {
      return false;
    }
    out = v;
  } catch (...) {
    return false;
  }
  return true;
}

int usage() {
  std::cerr <<
      R"USAGE(usage:
  robot_config_client get
  robot_config_client update --expected-version N
                            [--rate HZ] [--buffer SAMPLES] [--latency MS]
                            [--node /robot_config]

At least one of --rate/--buffer/--latency must be given; omitted fields keep
their current value.
)USAGE";
  return 10;
}

}  // namespace

int main(int argc, char** argv) {
  rclcpp::init(argc, argv);
  auto node = std::make_shared<rclcpp::Node>("robot_config_cli");

  std::string node_name = "/robot_config";
  std::string command;
  std::uint64_t expected_version = 0;
  bool have_expected = false;
  bool set_rate = false, set_buffer = false, set_latency = false;
  double rate = 0.0;
  std::uint64_t buffer_u64 = 0, latency = 0;

  for (int i = 1; i < argc; ++i) {
    std::string arg = argv[i];
    auto next = [&](const char* opt) -> std::string {
      if (i + 1 >= argc) {
        std::cerr << "missing value for " << opt << "\n";
        std::exit(usage());
      }
      return argv[++i];
    };
    if (arg == "get" || arg == "update") {
      if (!command.empty()) {
        return usage();
      }
      command = arg;
    } else if (arg == "--node") {
      node_name = next("--node");
    } else if (arg == "--expected-version") {
      if (!parse_u64(next("--expected-version"), expected_version)) {
        std::cerr << "bad --expected-version\n";
        return 10;
      }
      have_expected = true;
    } else if (arg == "--rate") {
      if (!parse_double(next("--rate"), rate)) {
        std::cerr << "bad --rate\n";
        return 10;
      }
      set_rate = true;
    } else if (arg == "--buffer") {
      if (!parse_u64(next("--buffer"), buffer_u64) ||
          buffer_u64 > 0xffffffffull) {
        std::cerr << "bad --buffer (must fit in uint32)\n";
        return 10;
      }
      set_buffer = true;
    } else if (arg == "--latency") {
      if (!parse_u64(next("--latency"), latency)) {
        std::cerr << "bad --latency\n";
        return 10;
      }
      set_latency = true;
    } else {
      std::cerr << "unknown argument: " << arg << "\n";
      return usage();
    }
  }

  if (command.empty()) {
    return usage();
  }

  int exit_code = 0;

  if (command == "get") {
    auto client = node->create_client<robot_param_atomic::srv::GetConfig>(
        node_name + "/get");
    if (!client->wait_for_service(kWaitTimeout)) {
      std::cerr << "service " << node_name
                << "/get not available after 10s; is robot_config_node running?\n";
      rclcpp::shutdown();
      return 10;
    }
    auto req = std::make_shared<robot_param_atomic::srv::GetConfig::Request>();
    auto fut = client->async_send_request(req);
    if (rclcpp::spin_until_future_complete(node, fut, kWaitTimeout) !=
        rclcpp::FutureReturnCode::SUCCESS) {
      std::cerr << "call timed out\n";
      rclcpp::shutdown();
      return 10;
    }
    auto resp = fut.get();
    std::cout << "{\n"
              << "  \"version\": " << resp->version << ",\n"
              << "  \"sample_rate_hz\": " << std::setprecision(17)
              << resp->sample_rate_hz << ",\n"
              << "  \"buffer_length\": " << resp->buffer_length << ",\n"
              << "  \"allowed_latency_ms\": " << resp->allowed_latency_ms << ",\n"
              << "  \"committed_at_ms\": " << resp->committed_at_ms << ",\n"
              << "  \"load_state\": \"" << load_state_name(resp->load_state)
              << "\",\n"
              << "  \"load_detail\": \"" << resp->load_detail << "\"\n"
              << "}\n";
  } else {
    if (!have_expected || !(set_rate || set_buffer || set_latency)) {
      std::cerr << "update requires --expected-version and at least one "
                   "of --rate/--buffer/--latency\n";
      rclcpp::shutdown();
      return usage();
    }
    auto client = node->create_client<robot_param_atomic::srv::AtomicUpdate>(
        node_name + "/update");
    if (!client->wait_for_service(kWaitTimeout)) {
      std::cerr << "service " << node_name
                << "/update not available after 10s\n";
      rclcpp::shutdown();
      return 10;
    }
    auto req =
        std::make_shared<robot_param_atomic::srv::AtomicUpdate::Request>();
    req->expected_version = expected_version;
    req->set_sample_rate = set_rate;
    req->sample_rate_hz = rate;
    req->set_buffer_length = set_buffer;
    req->buffer_length = static_cast<std::uint32_t>(buffer_u64);
    req->set_allowed_latency = set_latency;
    req->allowed_latency_ms = latency;

    auto fut = client->async_send_request(req);
    if (rclcpp::spin_until_future_complete(node, fut, kWaitTimeout) !=
        rclcpp::FutureReturnCode::SUCCESS) {
      std::cerr << "call timed out\n";
      rclcpp::shutdown();
      return 10;
    }
    auto resp = fut.get();
    std::cout << "{\n"
              << "  \"status\": " << static_cast<int>(resp->status)
              << ",  // " << status_name(resp->status) << "\n"
              << "  \"message\": \"" << resp->message << "\",\n"
              << "  \"version\": " << resp->version << ",\n"
              << "  \"sample_rate_hz\": " << std::setprecision(17)
              << resp->sample_rate_hz << ",\n"
              << "  \"buffer_length\": " << resp->buffer_length << ",\n"
              << "  \"allowed_latency_ms\": " << resp->allowed_latency_ms << ",\n"
              << "  \"committed_at_ms\": " << resp->committed_at_ms << "\n"
              << "}\n";
    exit_code = resp->status == 0 ? 0 : static_cast<int>(resp->status);
  }

  rclcpp::shutdown();
  return exit_code;
}
