// SPDX-License-Identifier: Apache-2.0

#include "robot_param_atomic/config_node.hpp"

#include <set>
#include <stdexcept>
#include <utility>
#include <vector>

#include <rclcpp/rclcpp.hpp>

namespace robot_param_atomic
{

namespace
{

constexpr const char * kServiceUpdate = "~/update";
constexpr const char * kServiceGet = "~/get";

// Built-in defaults, used on first boot and when every persisted record is
// corrupt. Valid by construction: 1.0 >= 2 * 0.25.
Snapshot default_snapshot()
{
  Snapshot d;
  d.sampling_rate_hz = 100.0;
  d.cache_length_s = 1.0;
  d.allowed_latency_s = 0.25;
  return d;
}

// Map store-level result codes onto the service constants.
uint8_t to_srv_code(CommitCode c)
{
  switch (c) {
    case CommitCode::OK: return srv::UpdateConfig::Response::OK;
    case CommitCode::REJECT_CONSTRAINT:
      return srv::UpdateConfig::Response::REJECT_CONSTRAINT;
    case CommitCode::REJECT_STALE_VERSION:
      return srv::UpdateConfig::Response::REJECT_STALE_VERSION;
    case CommitCode::REJECT_UPDATE_IN_CALLBACK:
      return srv::UpdateConfig::Response::REJECT_UPDATE_IN_CALLBACK;
    case CommitCode::ERR_PERSISTENCE:
      return srv::UpdateConfig::Response::ERR_PERSISTENCE;
    case CommitCode::ERR_INTERNAL:
      return srv::UpdateConfig::Response::ERR_INTERNAL;
  }
  return srv::UpdateConfig::Response::ERR_INTERNAL;
}

}  // namespace

ConfigNode::ConfigNode(const rclcpp::NodeOptions & options)
: rclcpp::Node("robot_config", options)
{
  // ---- Declare configuration parameters -----------------------------------
  // db_path is the persistence location. The three domain parameters are the
  // current committed snapshot; they are kept READ-ONLY via the parameter
  // callback below: all writes must go through the atomic service so that the
  // cross-field invariant and version check can be enforced atomically.
  const Snapshot defaults = default_snapshot();

  this->declare_parameter<std::string>("db_path", "config_atomic.db");
  this->declare_parameter<double>("sampling_rate_hz", defaults.sampling_rate_hz);
  this->declare_parameter<double>("cache_length_s", defaults.cache_length_s);
  this->declare_parameter<double>("allowed_latency_s", defaults.allowed_latency_s);
  // Test-only switch (default off): makes the change callback attempt a
  // nested commit so REJECT_UPDATE_IN_CALLBACK can be verified end-to-end.
  this->declare_parameter<bool>("probe_callback_commit", false);
  probe_callback_commit_ = this->get_parameter("probe_callback_commit").as_bool();

  // Reject direct parameter writes: they would bypass batching, validation
  // and the version token. Internal mirror updates set applying_internal_.
  // This is a PRE-set ("on set") callback, so returning unsuccessful actually
  // blocks the write (unlike a post-set callback, which in Jazzy cannot).
  const std::set<std::string> guarded = {
    "sampling_rate_hz", "cache_length_s", "allowed_latency_s", "db_path"};
  guarded_params_cb_ = this->add_on_set_parameters_callback(
    [this, guarded](const std::vector<rclcpp::Parameter> & params) {
      rcl_interfaces::msg::SetParametersResult result;
      result.successful = true;
      if (applying_internal_.load()) {
        return result;  // our own mirror update
      }
      for (const auto & p : params) {
        if (guarded.count(p.get_name()) != 0) {
          result.successful = false;
          result.reason =
            "parameter '" + p.get_name() +
            "' is managed by the atomic config service and must not be set "
            "directly; call the '" +
            std::string(kServiceUpdate) +
            "' service with the whole snapshot and expected_version";
          RCLCPP_WARN(
            this->get_logger(), "rejected direct set of '%s'",
            p.get_name().c_str());
          return result;
        }
      }
      return result;
    });

  // ---- Open the persistent store and recover ------------------------------
  store_ = std::make_unique<ConfigStore>();
  const std::string db_path =
    this->get_parameter("db_path").as_string();

  // Extra listeners run after the ROS-param mirror, still under the store
  // lock, and receive the same immutable old/new snapshots the store used.
  store_->set_callback(
    [this](const Snapshot & prev, const Snapshot & next, double ver) {
      // Contract: a callback observes one complete immutable snapshot. We read
      // prev/next here (no torn fields). If the probe is enabled, attempt to
      // write from inside the callback: this must be rejected.
      if (probe_callback_commit_) {
        Snapshot illegal = next;
        const CommitResult probed = store_->commit(ver, illegal, false);
        last_callback_probe_code_.store(static_cast<int>(probed.code));
        if (probed.code == CommitCode::REJECT_UPDATE_IN_CALLBACK) {
          RCLCPP_WARN(
            this->get_logger(),
            "[callback] REJECT_UPDATE_IN_CALLBACK: a commit attempted from "
            "within a change callback was rejected; snapshot v%.0f retained",
            ver);
        } else {
          RCLCPP_ERROR(
            this->get_logger(),
            "callback re-entrancy probe: expected REJECT_UPDATE_IN_CALLBACK "
            "(3), got code %d",
            static_cast<int>(probed.code));
        }
      }
      mirror_snapshot_to_ros_params(next);
      for (auto & l : extra_listeners_) {
        if (l) {
          l(prev, next, ver);
        }
      }
    });

  try {
    load_outcome_ = store_->open(db_path, defaults);
  } catch (const std::exception & e) {
    RCLCPP_FATAL(
      this->get_logger(), "failed to open configuration database %s: %s",
      db_path.c_str(), e.what());
    throw;
  }

  // Log the recovery status explicitly — a rejected corrupt record is never
  // silent.
  switch (load_outcome_.status) {
    case LoadStatus::FRESH:
      RCLCPP_INFO(this->get_logger(), "[load] FRESH: %s", load_outcome_.message.c_str());
      break;
    case LoadStatus::LOADED:
      RCLCPP_INFO(this->get_logger(), "[load] LOADED: %s", load_outcome_.message.c_str());
      break;
    case LoadStatus::CORRUPT_REJECTED:
      RCLCPP_ERROR(
        this->get_logger(), "[load] CORRUPT_REJECTED: %s",
        load_outcome_.message.c_str());
      break;
  }

  // Mirror the loaded snapshot into ROS parameters (internal -> allowed).
  mirror_snapshot_to_ros_params(load_outcome_.snapshot);

  RCLCPP_INFO(
    this->get_logger(),
    "current config v%.0f: sampling_rate_hz=%.6g cache_length_s=%.6g "
    "allowed_latency_s=%.6g sha256=%s",
    load_outcome_.version, load_outcome_.snapshot.sampling_rate_hz,
    load_outcome_.snapshot.cache_length_s,
    load_outcome_.snapshot.allowed_latency_s, load_outcome_.hash.c_str());

  // ---- Services -----------------------------------------------------------
  using namespace std::placeholders;
  update_srv_ = this->create_service<srv::UpdateConfig>(
    kServiceUpdate,
    std::bind(&ConfigNode::handle_update, this, _1, _2));
  get_srv_ = this->create_service<srv::GetConfig>(
    kServiceGet,
    std::bind(&ConfigNode::handle_get, this, _1, _2));

  RCLCPP_INFO(
    this->get_logger(), "atomic configuration ready: service '%s' / '%s'",
    kServiceUpdate, kServiceGet);
}

void ConfigNode::add_change_listener(ChangeCallback listener)
{
  extra_listeners_.push_back(std::move(listener));
}

void ConfigNode::mirror_snapshot_to_ros_params(const Snapshot & s)
{
  applying_internal_.store(true);
  this->set_parameter(rclcpp::Parameter("sampling_rate_hz", s.sampling_rate_hz));
  this->set_parameter(rclcpp::Parameter("cache_length_s", s.cache_length_s));
  this->set_parameter(rclcpp::Parameter("allowed_latency_s", s.allowed_latency_s));
  applying_internal_.store(false);
}

void ConfigNode::handle_update(
  const std::shared_ptr<srv::UpdateConfig::Request> req,
  std::shared_ptr<srv::UpdateConfig::Response> resp)
{
  Snapshot candidate;
  candidate.sampling_rate_hz = req->sampling_rate_hz;
  candidate.cache_length_s = req->cache_length_s;
  candidate.allowed_latency_s = req->allowed_latency_s;

  // -1.0 on a field means the client wants to keep it; that is a partial
  // update, validated against the resulting whole snapshot.
  const bool partial =
    candidate.sampling_rate_hz == ConfigStore::KEEP ||
    candidate.cache_length_s == ConfigStore::KEEP ||
    candidate.allowed_latency_s == ConfigStore::KEEP;

  const CommitResult r =
    store_->commit(req->expected_version, candidate, partial);

  resp->ok = r.ok();
  resp->code = to_srv_code(r.code);
  resp->message = r.message;
  resp->version = r.version;
  resp->sampling_rate_hz = r.snapshot.sampling_rate_hz;
  resp->cache_length_s = r.snapshot.cache_length_s;
  resp->allowed_latency_s = r.snapshot.allowed_latency_s;

  if (r.ok()) {
    RCLCPP_INFO(
      this->get_logger(),
      "[commit] %s -> sampling_rate_hz=%.6g cache_length_s=%.6g "
      "allowed_latency_s=%.6g",
      r.message.c_str(), r.snapshot.sampling_rate_hz, r.snapshot.cache_length_s,
      r.snapshot.allowed_latency_s);
  } else {
    RCLCPP_WARN(
      this->get_logger(), "[reject code=%u] %s",
      static_cast<unsigned>(r.code), r.message.c_str());
  }
}

void ConfigNode::handle_get(
  const std::shared_ptr<srv::GetConfig::Request>,
  std::shared_ptr<srv::GetConfig::Response> resp)
{
  const Snapshot s = store_->current_snapshot();
  resp->version = store_->current_version();
  resp->sampling_rate_hz = s.sampling_rate_hz;
  resp->cache_length_s = s.cache_length_s;
  resp->allowed_latency_s = s.allowed_latency_s;
  resp->config_hash = store_->current_hash();
  resp->commit_seq = store_->commit_seq();
}

}  // namespace robot_param_atomic
