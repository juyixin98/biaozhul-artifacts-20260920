// SPDX-License-Identifier: Apache-2.0
#include <memory>

#include <rclcpp/rclcpp.hpp>

#include "robot_param/node/robot_config_node.hpp"

int main(int argc, char** argv) {
  rclcpp::init(argc, argv);

  auto node = std::make_shared<robot_param::RobotConfigNode>();

  // MultiThreadedExecutor is mandatory: change callbacks must not block
  // service processing, and updates from callbacks must be answered.
  rclcpp::executors::MultiThreadedExecutor exec(rclcpp::ExecutorOptions(), 4);
  exec.add_node(node);

  RCLCPP_INFO(node->get_logger(),
              "robot_param_atomic node ready; services ~/update and ~/get");
  exec.spin();

  rclcpp::shutdown();
  return 0;
}
