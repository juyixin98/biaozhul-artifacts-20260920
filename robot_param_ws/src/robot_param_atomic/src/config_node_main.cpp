// SPDX-License-Identifier: Apache-2.0
/// @brief Entry point for the robot atomic-configuration node.
#include <memory>

#include <rclcpp/rclcpp.hpp>

#include "robot_param_atomic/config_node.hpp"

int main(int argc, char ** argv)
{
  rclcpp::init(argc, argv);
  try {
    auto node = std::make_shared<robot_param_atomic::ConfigNode>();
    // Multi-threaded executor so long-running service callbacks (and their
    // serialised change callbacks) do not block each other.
    rclcpp::executors::MultiThreadedExecutor executor(
      rclcpp::ExecutorOptions(), 4);
    executor.add_node(node);
    executor.spin();
  } catch (const std::exception & e) {
    RCLCPP_FATAL(rclcpp::get_logger("robot_config"), "node aborted: %s", e.what());
    rclcpp::shutdown();
    return 1;
  }
  rclcpp::shutdown();
  return 0;
}
