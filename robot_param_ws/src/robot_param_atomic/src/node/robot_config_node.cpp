// SPDX-License-Identifier: Apache-2.0
#include "robot_param/node/robot_config_node.hpp"

#include <sys/stat.h>

#include <chrono>
#include <cstdlib>
#include <fstream>
#include <iomanip>
#include <sstream>
#include <stdexcept>
#include <utility>

using namespace std::chrono_literals;

namespace robot_param {

namespace {

std::int64_t now_ms() {
  return std::chrono::duration_cast<std::chrono::milliseconds>(
             std::chrono::system_clock::now().time_since_epoch())
      .count();
}

template <typename RespT>
void fill_resp(RespT& resp, const ConfigSnapshot& s) {
  resp.version = s.version;
  resp.sample_rate_hz = s.sample_rate_hz;
  resp.buffer_length = s.buffer_length;
  resp.allowed_latency_ms = s.allowed_latency_ms;
  resp.committed_at_ms = s.committed_at_ms;
}

bool is_inproc(const std::string& origin) {
  return origin.rfind(RobotConfigNode::kInprocOriginPrefix, 0) == 0;
}

}  // namespace

RobotConfigNode::RobotConfigNode()
    : RobotConfigNode(rclcpp::NodeOptions{}) {}

RobotConfigNode::RobotConfigNode(const rclcpp::NodeOptions& options)
    : rclcpp::Node("robot_config", options) {
  db_path_ = this->declare_parameter<std::string>("db_path", kDefaultDbPath);
  std::string key_env =
      this->declare_parameter<std::string>("key_env", kDefaultKeyEnv);

  auto key = load_or_create_key(key_env);
  store_ = ConfigStore::open(db_path_, std::move(key));
  setup();

  dispatcher_ = std::thread(&RobotConfigNode::dispatcher_loop, this);

  // Publish the initial snapshot on the latched topic so late subscribers
  // immediately see v0 or the revision loaded from disk.
  snapshot_pub_->publish(to_msg(store_->snapshot()));

  const LoadInfo info = store_->load_info();
  if (info.state == LoadState::FRESH || info.state == LoadState::LOADED) {
    RCLCPP_INFO(this->get_logger(), "persistence: %s", info.detail.c_str());
  } else {
    RCLCPP_WARN(this->get_logger(), "persistence: %s", info.detail.c_str());
  }
}

RobotConfigNode::~RobotConfigNode() {
  {
    std::lock_guard<std::mutex> lk(queue_mutex_);
    dispatcher_stop_ = true;
  }
  queue_cv_.notify_all();
  if (dispatcher_.joinable()) {
    dispatcher_.join();
  }
}

void RobotConfigNode::setup() {
  // Reentrant group: concurrent service calls execute on different executor
  // threads. Serialization is enforced where it matters (ConfigStore), and no
  // user callback ever runs inside these handlers.
  srv_group_ = this->create_callback_group(
      rclcpp::CallbackGroupType::Reentrant);

  update_srv_ = this->create_service<srv::AtomicUpdate>(
      "~/update",
      std::bind(&RobotConfigNode::handle_update, this, std::placeholders::_1,
                std::placeholders::_2, std::placeholders::_3),
      rclcpp::ServicesQoS(), srv_group_);
  get_srv_ = this->create_service<srv::GetConfig>(
      "~/get",
      std::bind(&RobotConfigNode::handle_get, this, std::placeholders::_1,
                std::placeholders::_2, std::placeholders::_3),
      rclcpp::ServicesQoS(), srv_group_);

  // Keep the last few snapshots available to late-joining subscribers.
  rmw_qos_profile_t qos = rmw_qos_profile_default;
  qos.history = RMW_QOS_POLICY_HISTORY_KEEP_LAST;
  qos.depth = 4;
  qos.durability = RMW_QOS_POLICY_DURABILITY_TRANSIENT_LOCAL;
  qos.reliability = RMW_QOS_POLICY_RELIABILITY_RELIABLE;
  snapshot_pub_ = this->create_publisher<msg::ConfigSnapshot>(
      "~/snapshots", rclcpp::QoS(rclcpp::QoSInitialization::from_rmw(qos), qos));
}

std::vector<std::uint8_t> RobotConfigNode::load_or_create_key(
    const std::string& key_env) {
  // 1) Explicit key through the environment (raw bytes or 64 hex chars).
  if (const char* env = std::getenv(key_env.c_str());
      env != nullptr && env[0] != '\0') {
    RCLCPP_INFO(this->get_logger(), "using HMAC key from %s", key_env.c_str());
    return parse_key(env);
  }

  // 2) Key file next to the database: <db_path>.key.
  const std::string key_path = db_path_ + ".key";
  {
    std::ifstream in(key_path, std::ios::binary);
    if (in) {
      std::ostringstream ss;
      ss << in.rdbuf();
      std::string material = ss.str();
      // Tolerate trailing newlines in case the file was hand-edited.
      while (!material.empty() &&
             (material.back() == '\n' || material.back() == '\r')) {
        material.pop_back();
      }
      if (material.empty()) {
        throw std::runtime_error("HMAC key file " + key_path + " is empty");
      }
      std::vector<std::uint8_t> key = parse_key(material);

      // Defense in depth: an over-permissive key file is hardened, not trusted.
      struct stat st {};
      if (stat(key_path.c_str(), &st) == 0 &&
          (st.st_mode & (S_IRWXG | S_IRWXO)) != 0) {
        RCLCPP_WARN(this->get_logger(),
                    "key file %s is group/world accessible; tightening to 0600",
                    key_path.c_str());
        chmod(key_path.c_str(), S_IRUSR | S_IWUSR);
      }
      RCLCPP_INFO(this->get_logger(), "loaded HMAC key from %s",
                  key_path.c_str());
      return key;
    }
  }

  // 3) First start: generate a real random key and persist it 0600.
  std::vector<std::uint8_t> key = random_key_32();
  std::ofstream out(key_path, std::ios::binary | std::ios::trunc);
  if (!out) {
    throw std::runtime_error("cannot create HMAC key file " + key_path);
  }
  out.write(reinterpret_cast<const char*>(key.data()),
            static_cast<std::streamsize>(key.size()));
  out.flush();
  out.close();
  if (chmod(key_path.c_str(), S_IRUSR | S_IWUSR) != 0) {
    RCLCPP_WARN(this->get_logger(), "could not set permissions 0600 on %s",
                key_path.c_str());
  }
  RCLCPP_INFO(this->get_logger(),
              "generated new random HMAC key at %s (back it up; losing it "
              "invalidates old rows)",
              key_path.c_str());
  return key;
}

msg::ConfigSnapshot RobotConfigNode::to_msg(const ConfigSnapshot& s) {
  msg::ConfigSnapshot m;
  m.version = s.version;
  m.sample_rate_hz = s.sample_rate_hz;
  m.buffer_length = s.buffer_length;
  m.allowed_latency_ms = s.allowed_latency_ms;
  m.committed_at_ms = s.committed_at_ms;
  return m;
}

void RobotConfigNode::handle_update(
    const std::shared_ptr<rmw_request_id_t>,
    const std::shared_ptr<srv::AtomicUpdate::Request> req,
    std::shared_ptr<srv::AtomicUpdate::Response> resp) {
  // Callbacks only ever read snapshots. A same-process listener that attempts
  // to commit while its own snapshot is being delivered is rejected up front.
  // This is lock-free on purpose so it can never deadlock; external clients
  // (empty origin) are unaffected and simply serialize.
  if (dispatch_in_progress_.load(std::memory_order_acquire) &&
      is_inproc(req->origin)) {
    ConfigSnapshot cur = store_->snapshot();
    resp->status = REENTRANT_UPDATE;
    resp->message =
        "update issued from inside a change callback is not allowed; callbacks "
        "see a read-only snapshot - reissue the update from outside";
    fill_resp(*resp, cur);
    return;
  }

  ConfigPatch patch;
  patch.set_sample_rate = req->set_sample_rate;
  patch.sample_rate_hz = req->sample_rate_hz;
  patch.set_buffer_length = req->set_buffer_length;
  patch.buffer_length = req->buffer_length;
  patch.set_allowed_latency = req->set_allowed_latency;
  patch.allowed_latency_ms = req->allowed_latency_ms;

  // Short critical section inside the store: version check + validation +
  // durable transaction. No user code runs here.
  CommitResult result =
      store_->commit(req->expected_version, patch, now_ms());
  resp->status = static_cast<std::uint8_t>(result.status);
  resp->message = result.message;
  fill_resp(*resp, result.snapshot);

  if (result.status == CommitStatus::OK) {
    // Ordered, asynchronous delivery; the service replies immediately.
    enqueue_dispatch(result.snapshot);
  }
}

void RobotConfigNode::handle_get(
    const std::shared_ptr<rmw_request_id_t>,
    const std::shared_ptr<srv::GetConfig::Request>,
    std::shared_ptr<srv::GetConfig::Response> resp) {
  // The store has its own lock; reads answer even while dispatch is running.
  ConfigSnapshot s = store_->snapshot();
  fill_resp(*resp, s);
  const LoadInfo info = store_->load_info();
  resp->load_state = static_cast<std::uint8_t>(info.state);
  resp->load_detail = info.detail;
}

std::size_t RobotConfigNode::add_change_listener(ConfigCallback cb) {
  std::lock_guard<std::mutex> lk(listeners_mutex_);
  listeners_.push_back(std::move(cb));
  const std::size_t idx = listeners_.size() - 1;

  // Immediate replay of the current COMPLETE snapshot to the new listener.
  // This is not a "dispatch" in progress: listeners are free to call update
  // from here as long as no committed revision is being delivered.
  try {
    listeners_.back()(store_->snapshot());
  } catch (const std::exception& e) {
    RCLCPP_ERROR(this->get_logger(),
                 "change listener %zu threw during replay: %s (ignored)", idx,
                 e.what());
  } catch (...) {
    RCLCPP_ERROR(this->get_logger(),
                 "change listener %zu threw during replay (ignored)", idx);
  }
  return idx;
}

void RobotConfigNode::enqueue_dispatch(const ConfigSnapshot& s) {
  {
    std::lock_guard<std::mutex> lk(queue_mutex_);
    queue_.push_back(s);
  }
  queue_cv_.notify_one();
}

void RobotConfigNode::dispatcher_loop() {
  for (;;) {
    ConfigSnapshot s;
    {
      std::unique_lock<std::mutex> lk(queue_mutex_);
      queue_cv_.wait(lk, [this] { return dispatcher_stop_ || !queue_.empty(); });
      if (dispatcher_stop_ && queue_.empty()) {
        return;
      }
      s = queue_.front();
      queue_.pop_front();
    }

    dispatch_in_progress_.store(true, std::memory_order_release);
    deliver_one(s);
    dispatch_in_progress_.store(false, std::memory_order_release);
  }
}

void RobotConfigNode::deliver_one(const ConfigSnapshot& s) {
  // External consumers: one latched message per committed revision.
  snapshot_pub_->publish(to_msg(s));

  // In-process listeners get the same immutable snapshot. A throwing listener
  // must not break the others or the dispatcher.
  std::vector<ConfigCallback> listeners;
  {
    std::lock_guard<std::mutex> lk(listeners_mutex_);
    listeners = listeners_;
  }
  for (std::size_t i = 0; i < listeners.size(); ++i) {
    try {
      listeners[i](s);
    } catch (const std::exception& e) {
      RCLCPP_ERROR(this->get_logger(),
                   "change listener %zu threw: %s (ignored)", i, e.what());
    } catch (...) {
      RCLCPP_ERROR(this->get_logger(),
                   "change listener %zu threw an unknown exception (ignored)",
                   i);
    }
  }
}

}  // namespace robot_param
