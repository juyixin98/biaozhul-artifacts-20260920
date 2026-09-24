cmake_minimum_required(VERSION 3.16)
project(tf_tree_check LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

if(NOT CMAKE_BUILD_TYPE)
  set(CMAKE_BUILD_TYPE Release)
endif()

add_compile_options(-Wall -Wextra -Wpedantic)

# ---- 锁定依赖（vendored，见 ../DEPS.lock）----
set(THIRD_PARTY ${CMAKE_CURRENT_SOURCE_DIR}/third_party)
if(NOT EXISTS ${THIRD_PARTY}/eigen-3.4.0/Eigen/Dense)
  message(FATAL_ERROR
    "Eigen 未就位。请执行: cd third_party && tar xzf eigen-3.4.0.tar.gz")
endif()

add_library(eigen INTERFACE)
target_include_directories(eigen INTERFACE ${THIRD_PARTY}/eigen-3.4.0)

add_library(nlohmann_json INTERFACE)
target_include_directories(nlohmann_json INTERFACE ${THIRD_PARTY})

# ---- 服务端 ----
add_executable(tf_server
  src/main.cpp
  src/api.cpp
  src/http_server.cpp
  src/transform_tree.cpp
  src/sha256.cpp
)
target_include_directories(tf_server PRIVATE src)
target_link_libraries(tf_server PRIVATE eigen nlohmann_json)
find_package(Threads REQUIRED)
target_link_libraries(tf_server PRIVATE Threads::Threads)

# ---- 测试 ----
enable_testing()
add_executable(tf_tests
  tests/test_main.cpp
  src/transform_tree.cpp
  src/sha256.cpp
)
target_include_directories(tf_tests PRIVATE src)
target_link_libraries(tf_tests PRIVATE eigen Threads::Threads)
add_test(NAME tf_tests COMMAND tf_tests)
