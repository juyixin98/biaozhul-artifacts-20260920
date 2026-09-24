// SPDX-License-Identifier: Apache-2.0
/// @file config_node.hpp
/// @brief ROS2 node wrapping ConfigStore in atomic configuration services.
#pragma once

#include <atomic>
#include <memory>
#include <string>
#include <vector>

#include <rclcpp/rclcpp.hpp>

#include "robot_param_atomic/config_store.hpp"
#include "robot_param_atomic/srv/get_config.hpp"
#include "robot_param_atomic/srv/update_config.hpp"

namespace robot_param_atomic
{

class ConfigNode : public rclcpp::Node
{
public:
  explicit ConfigNode(const rclcpp::NodeOptions & options = rclcpp::NodeOptions());

  // The outcome of the startup persistence check, for tests and diagnostics.
  const LoadOutcome & load_outcome() const { return load_outcome_; }

  // Test-only: if the parameter "probe_callback_commit" was true at startup,
  // the node's own change callback attempts a nested commit and remembers the
  // resulting code. Returns the last probed code (or -1 when the probe is
  // disabled / no commit has happened yet).
  int last_callback_probe_code() const noexcept
  {
    return last_callback_probe_code_.load();
  }

  // Extra observers notified, under the store mutex, after each committed
  // change. Useful for components that must react to a new immutable snapshot.
  // Listeners MUST be cheap and MUST NOT commit to the same store.
  void add_change_listener(ChangeCallback listener);

private:
  void handle_update(
    const std::shared_ptr<srv::UpdateConfig::Request> req,
    std::shared_ptr<srv::UpdateConfig::Response> resp);
  void handle_get(
    const std::shared_ptr<srv::GetConfig::Request> req,
    std::shared_ptr<srv::GetConfig::Response> resp);

  // Reflect a committed snapshot into the (otherwise read-only) ROS
  // parameters so generic tooling (`ros2 param get`) observes the truth.
  void mirror_snapshot_to_ros_params(const Snapshot & s);

  std::unique_ptr<ConfigStore> store_;
  LoadOutcome load_outcome_;
  std::atomic<bool> applying_internal_{false};
  std::atomic<int> last_callback_probe_code_{-1};
  bool probe_callback_commit_ = false;
  std::vector<ChangeCallback> extra_listeners_;

  rclcpp::Service<srv::UpdateConfig>::SharedPtr update_srv_;
  rclcpp::Service<srv::GetConfig>::SharedPtr get_srv_;
  rclcpp::node_interfaces::OnSetParametersCallbackHandle::SharedPtr
    guarded_params_cb_;
};

}  // namespace robot_param_atomic
