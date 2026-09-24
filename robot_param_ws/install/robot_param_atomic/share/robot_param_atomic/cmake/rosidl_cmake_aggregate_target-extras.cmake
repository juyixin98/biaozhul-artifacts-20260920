# generated from rosidl_cmake/cmake/rosidl_cmake_aggregate_target-extras.cmake.in

# Create a convenience aggregate target robot_param_atomic::robot_param_atomic
# that links all generated interface targets, so downstream packages can use
# a single modern CMake target name instead of ${robot_param_atomic_TARGETS}.
if(robot_param_atomic_TARGETS AND NOT TARGET robot_param_atomic::robot_param_atomic)
  add_library(robot_param_atomic::robot_param_atomic INTERFACE IMPORTED)
  set_target_properties(robot_param_atomic::robot_param_atomic PROPERTIES
    INTERFACE_LINK_LIBRARIES "${robot_param_atomic_TARGETS}")
endif()
