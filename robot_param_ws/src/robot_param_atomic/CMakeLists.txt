cmake_minimum_required(VERSION 3.16)
project(robot_param_atomic VERSION 1.0.0 LANGUAGES C CXX)

if(NOT CMAKE_CXX_STANDARD)
  set(CMAKE_CXX_STANDARD 17)
  set(CMAKE_CXX_STANDARD_REQUIRED ON)
  set(CMAKE_CXX_EXTENSIONS OFF)
endif()

if(CMAKE_COMPILER_IS_GNUCXX OR CMAKE_CXX_COMPILER_ID MATCHES "Clang")
  add_compile_options(-Wall -Wextra -Wpedantic)
endif()

find_package(ament_cmake REQUIRED)
find_package(rclcpp REQUIRED)
find_package(rosidl_default_generators REQUIRED)
find_package(SQLite3 REQUIRED)
find_package(OpenSSL REQUIRED)

rosidl_generate_interfaces(${PROJECT_NAME}
  "msg/ConfigSnapshot.msg"
  "srv/AtomicUpdate.srv"
  "srv/GetConfig.srv"
  ADD_LINTER_TESTS
)

# ---- ROS-independent core: validation + real crypto + SQLite store ----
add_library(${PROJECT_NAME}_core SHARED
  src/core/validation.cpp
  src/core/crypto.cpp
  src/core/store.cpp
)
target_include_directories(${PROJECT_NAME}_core PUBLIC
  $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
  $<INSTALL_INTERFACE:include/${PROJECT_NAME}>
)
target_link_libraries(${PROJECT_NAME}_core PUBLIC SQLite::SQLite3 OpenSSL::Crypto)
ament_export_targets(export_${PROJECT_NAME}_core HAS_LIBRARY_TARGET)

# ---- ROS 2 node (static lib so integration tests can instantiate it in-process) ----
add_library(${PROJECT_NAME}_node STATIC
  src/node/robot_config_node.cpp
)
target_include_directories(${PROJECT_NAME}_node PUBLIC
  $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
)
target_link_libraries(${PROJECT_NAME}_node ${PROJECT_NAME}_core OpenSSL::Crypto)
ament_target_dependencies(${PROJECT_NAME}_node rclcpp)
rosidl_get_typesupport_target(cpp_typesupport_target ${PROJECT_NAME} "rosidl_typesupport_cpp")
target_link_libraries(${PROJECT_NAME}_node "${cpp_typesupport_target}")

add_executable(robot_config_node src/node_main.cpp)
target_link_libraries(robot_config_node ${PROJECT_NAME}_node)

add_executable(robot_config_client src/client/robot_config_client.cpp)
target_link_libraries(robot_config_client ${PROJECT_NAME}_core)
ament_target_dependencies(robot_config_client rclcpp)
target_link_libraries(robot_config_client "${cpp_typesupport_target}")

install(TARGETS
  ${PROJECT_NAME}_core
  ${PROJECT_NAME}_node
  robot_config_node
  robot_config_client
  EXPORT export_${PROJECT_NAME}_core
  ARCHIVE DESTINATION lib
  LIBRARY DESTINATION lib
  RUNTIME DESTINATION lib/${PROJECT_NAME}
)
install(DIRECTORY include/ DESTINATION include/${PROJECT_NAME})

install(PROGRAMS scripts/smoke_e2e.sh scripts/restart_e2e.sh
  DESTINATION lib/${PROJECT_NAME})

if(BUILD_TESTING)
  find_package(ament_cmake_gtest REQUIRED)

  ament_add_gtest(test_validation test/test_validation.cpp)
  if(TARGET test_validation)
    target_link_libraries(test_validation ${PROJECT_NAME}_core)
  endif()

  ament_add_gtest(test_crypto test/test_crypto.cpp)
  if(TARGET test_crypto)
    target_link_libraries(test_crypto ${PROJECT_NAME}_core)
  endif()

  ament_add_gtest(test_store test/test_store.cpp)
  if(TARGET test_store)
    target_link_libraries(test_store ${PROJECT_NAME}_core)
  endif()

  ament_add_gtest(test_config_node_integration test/test_config_node_integration.cpp)
  if(TARGET test_config_node_integration)
    target_link_libraries(test_config_node_integration ${PROJECT_NAME}_node)
    ament_target_dependencies(test_config_node_integration rclcpp)
  endif()
endif()

ament_package()
