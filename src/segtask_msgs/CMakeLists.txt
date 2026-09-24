cmake_minimum_required(VERSION 3.8)
project(segtask_msgs)

if(NOT CMAKE_C_STANDARD)
  set(CMAKE_C_STANDARD 99)
endif()
if(NOT CMAKE_CXX_STANDARD)
  set(CMAKE_CXX_STANDARD 17)
endif()

find_package(ament_cmake REQUIRED)
find_package(rosidl_default_generators REQUIRED)
find_package(action_msgs REQUIRED)
find_package(builtin_interfaces REQUIRED)

rosidl_generate_interfaces(${PROJECT_NAME}
  "action/SegmentTask.action"
  "msg/GoalInfo.msg"
  "srv/QueryGoal.srv"
  "srv/ListGoals.srv"
  "srv/AdminRequest.srv"
  DEPENDENCIES builtin_interfaces action_msgs
  ADD_LINTER_TESTS
)

ament_package()
