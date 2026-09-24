// SPDX-License-Identifier: Apache-2.0
#pragma once

#include <atomic>
#include <condition_variable>
#include <cstdint>
#include <deque>
#include <functional>
#include <memory>
#include <mutex>
#include <thread>
#include <vector>

#include <rclcpp/rclcpp.hpp>

#include "robot_param/core/store.hpp"
#include "robot_param_atomic/msg/config_snapshot.hpp"
#include "robot_param_atomic/srv/atomic_update.hpp"
#include "robot_param_atomic/srv/get_config.hpp"

namespace robot_param {

namespace msg = robot_param_atomic::msg;
namespace srv = robot_param_atomic::srv;

using ConfigCallback = std::function<void(const ConfigSnapshot&)>;

// ROS 2 wrapper around ConfigStore.
//
//  * ~/update     (AtomicUpdate)  - atomic, version-checked batch updates
//  * ~/get        (GetConfig)     - read current snapshot and startup status
//  * ~/snapshots  (ConfigSnapshot, transient_local) - one event per commit
//
// Concurrency model
// -----------------
// The node MUST be spun by a MultiThreadedExecutor and its services live in a
// Reentrant group. The actual linearization is done inside ConfigStore (mutex)
// during a short critical section in the service handler; the handler never
// runs user callbacks, so it can never deadlock.
//
// Every committed snapshot is handed to a single dedicated dispatcher thread
// through a FIFO queue. That thread (a) publishes on ~/snapshots and
// (b) invokes the in-process listeners in registration order. Listeners
// therefore always observe complete, immutable snapshots, strictly in
// revision order, with no partial batch visible.
//
// A change listener that calls ~/update (same process) is identified by the
// "inproc:" origin prefix and answered REENTRANT_UPDATE while a dispatch is
// in progress: callbacks have read-only access to the snapshot and must not
// mutate it. Because the handler itself never runs listeners, this rejection
// cannot deadlock. External clients (empty origin), including other ROS nodes
// and the CLI, are never rejected as reentrant - their commits are serialized
// normally and simply arrive later in the FIFO.
class RobotConfigNode : public rclcpp::Node {
 public:
  static constexpr std::uint8_t REENTRANT_UPDATE = 4;
  static constexpr const char* kInprocOriginPrefix = "inproc:";

  static constexpr const char* kDefaultDbPath = "robot_param.sqlite3";
  static constexpr const char* kDefaultKeyEnv = "ROBOT_PARAM_KEY";

  RobotConfigNode();
  explicit RobotConfigNode(const rclcpp::NodeOptions& options);
  ~RobotConfigNode() override;

  // Registers an in-process listener. The new listener is invoked immediately
  // with the current complete snapshot and then once per committed revision.
  // Must be registered before spinning (safe afterwards as well).
  std::size_t add_change_listener(ConfigCallback cb);

  const ConfigStore& store() const { return *store_; }

 private:
  void setup();
  void handle_update(
      const std::shared_ptr<rmw_request_id_t> request_header,
      const std::shared_ptr<srv::AtomicUpdate::Request> req,
      std::shared_ptr<srv::AtomicUpdate::Response> resp);
  void handle_get(const std::shared_ptr<rmw_request_id_t> request_header,
                  const std::shared_ptr<srv::GetConfig::Request> req,
                  std::shared_ptr<srv::GetConfig::Response> resp);

  std::vector<std::uint8_t> load_or_create_key(const std::string& key_env);
  static msg::ConfigSnapshot to_msg(const ConfigSnapshot& s);

  void enqueue_dispatch(const ConfigSnapshot& s);
  void dispatcher_loop();
  void deliver_one(const ConfigSnapshot& s);

  std::unique_ptr<ConfigStore> store_;
  std::string db_path_;

  rclcpp::CallbackGroup::SharedPtr srv_group_;
  rclcpp::Service<srv::AtomicUpdate>::SharedPtr update_srv_;
  rclcpp::Service<srv::GetConfig>::SharedPtr get_srv_;
  rclcpp::Publisher<msg::ConfigSnapshot>::SharedPtr snapshot_pub_;

  std::mutex listeners_mutex_;
  std::vector<ConfigCallback> listeners_;

  // FIFO snapshot dispatch: one thread, strictly ordered delivery.
  std::mutex queue_mutex_;
  std::condition_variable queue_cv_;
  std::deque<ConfigSnapshot> queue_;
  bool dispatcher_stop_ = false;
  std::thread dispatcher_;
  std::atomic<bool> dispatch_in_progress_{false};
};

}  // namespace robot_param
