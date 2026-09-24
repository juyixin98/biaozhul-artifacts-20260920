cmake_minimum_required(VERSION 3.8)
project(action_cancel_interfaces)

if(NOT CMAKE_CXX_STANDARD)
  set(CMAKE_CXX_STANDARD 17)
endif()

find_package(ament_cmake REQUIRED)
find_package(rosidl_default_generators REQUIRED)
find_package(action_msgs REQUIRED)

rosidl_generate_interfaces(${PROJECT_NAME}
  "msg/SegmentRecord.msg"
  "msg/GoalSummary.msg"
  "action/SegmentedTask.action"
  "srv/GetGoal.srv"
  "srv/ListGoals.srv"
  DEPENDENCIES action_msgs
)

ament_package()
