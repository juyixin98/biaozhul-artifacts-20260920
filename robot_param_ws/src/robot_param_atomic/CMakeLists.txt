cmake_minimum_required(VERSION 3.22)
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

# Real cryptographic + storage backends (not mocked):
find_package(PkgConfig REQUIRED)
pkg_check_modules(SQLITE3 REQUIRED sqlite3)
find_package(OpenSSL 3.0 REQUIRED COMPONENTS Crypto)

# --- Service interfaces -----------------------------------------------------
rosidl_generate_interfaces(${PROJECT_NAME}
  "srv/UpdateConfig.srv"
  "srv/GetConfig.srv"
)

# --- Core library (pure C++ store + ROS node) ------------------------------
add_library(${PROJECT_NAME}_core SHARED
  src/config_store.cpp
  src/config_node.cpp
)
target_include_directories(${PROJECT_NAME}_core PUBLIC
  "$<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>"
  "$<INSTALL_INTERFACE:include/${PROJECT_NAME}>"
  ${SQLITE3_INCLUDE_DIRS}
)
target_link_directories(${PROJECT_NAME}_core PUBLIC ${SQLITE3_LIBRARY_DIRS})
target_link_libraries(${PROJECT_NAME}_core
  ${SQLITE3_LIBRARIES}
  OpenSSL::Crypto
)
ament_target_dependencies(${PROJECT_NAME}_core rclcpp)

# Link the generated C++ service typesupport into the library.
rosidl_get_typesupport_target(cpp_typesupport_target
  ${PROJECT_NAME} "rosidl_typesupport_cpp")
target_link_libraries(${PROJECT_NAME}_core "${cpp_typesupport_target}")

# --- Executables ------------------------------------------------------------
add_executable(robot_config_node src/config_node_main.cpp)
target_link_libraries(robot_config_node ${PROJECT_NAME}_core)

add_executable(robot_config_client src/config_client_main.cpp)
target_link_libraries(robot_config_client ${PROJECT_NAME}_core)

# --- Install ----------------------------------------------------------------
install(TARGETS ${PROJECT_NAME}_core robot_config_node robot_config_client
  EXPORT export_${PROJECT_NAME}
  ARCHIVE DESTINATION lib
  LIBRARY DESTINATION lib
  RUNTIME DESTINATION lib/${PROJECT_NAME}
)
install(DIRECTORY include/
  DESTINATION include/${PROJECT_NAME}
)
install(DIRECTORY launch config examples
  DESTINATION share/${PROJECT_NAME}
  OPTIONAL
)
install(PROGRAMS scripts/acceptance.sh
  DESTINATION lib/${PROJECT_NAME}
  RENAME acceptance
)

ament_export_dependencies(rclcpp rosidl_default_runtime)
ament_export_targets(export_${PROJECT_NAME})

# --- Tests ------------------------------------------------------------------
if(BUILD_TESTING)
  find_package(ament_lint_auto QUIET)
  # Lint is opt-in; it must never block the build of the functional tests.
  option(ROBOT_PARAM_RUN_LINT "Run ament linters" OFF)
  if(ament_lint_auto_FOUND AND ROBOT_PARAM_RUN_LINT)
    find_package(ament_lint_auto REQUIRED)
    ament_lint_auto_find_test_dependencies()
  endif()

  find_package(ament_cmake_gtest REQUIRED)

  ament_add_gtest(test_config_store test/test_config_store.cpp)
  if(TARGET test_config_store)
    target_link_libraries(test_config_store ${PROJECT_NAME}_core)
  endif()

  ament_add_gtest(test_config_node_integration
    test/test_config_node_integration.cpp
    TIMEOUT 60)
  if(TARGET test_config_node_integration)
    target_link_libraries(test_config_node_integration ${PROJECT_NAME}_core)
  endif()
endif()

ament_package()
