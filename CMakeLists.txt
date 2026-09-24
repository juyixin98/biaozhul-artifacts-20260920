cmake_minimum_required(VERSION 3.16)
project(tf_tree_server LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

if(NOT CMAKE_BUILD_TYPE)
  set(CMAKE_BUILD_TYPE Release)
endif()

set(CMAKE_RUNTIME_OUTPUT_DIRECTORY ${CMAKE_BINARY_DIR}/bin)

add_compile_options(-Wall -Wextra -Wpedantic)

find_package(OpenSSL 3 REQUIRED)
find_package(Threads REQUIRED)

# Vendored, hash-locked dependencies (see deps/SHA256SUMS)
add_library(eigen_vendor INTERFACE)
target_include_directories(eigen_vendor SYSTEM INTERFACE ${CMAKE_SOURCE_DIR}/third_party/eigen)

add_library(json_vendor INTERFACE)
target_include_directories(json_vendor SYSTEM INTERFACE ${CMAKE_SOURCE_DIR}/third_party/json)

add_library(tfcore STATIC
  src/frame_tree.cpp
  src/crypto.cpp
  src/http_server.cpp
  src/app.cpp
)
target_include_directories(tfcore PUBLIC src)
target_link_libraries(tfcore
  PUBLIC eigen_vendor json_vendor OpenSSL::Crypto Threads::Threads
)

add_executable(tf_server src/main.cpp)
target_link_libraries(tf_server PRIVATE tfcore)

enable_testing()

add_executable(test_frame_tree tests/cpp/test_frame_tree.cpp)
target_link_libraries(test_frame_tree PRIVATE tfcore)
add_test(NAME frame_tree_unit COMMAND test_frame_tree)

add_test(
  NAME http_integration
  COMMAND ${CMAKE_COMMAND} -E env
          TF_SERVER=$<TARGET_FILE:tf_server>
          python3 ${CMAKE_SOURCE_DIR}/tests/http/test_http.py
)
