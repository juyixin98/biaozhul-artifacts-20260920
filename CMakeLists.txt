cmake_minimum_required(VERSION 3.16)
project(joint_trajectory_parameterization LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

if(NOT CMAKE_BUILD_TYPE)
  set(CMAKE_BUILD_TYPE Release)
endif()

add_compile_options(-Wall -Wextra -Wpedantic)

# ---- Dependencies (pinned/verified by scripts/verify_dependencies.sh) ------
# Eigen 3: CMake package config
find_package(Eigen3 3.4.0 EXACT REQUIRED NO_MODULE)
# nlohmann/json 3.11.3 (header-only; the Ubuntu package ships no CMake config,
# so we check the version macros in the header directly).
find_path(NLOHMANN_JSON_INCLUDE_DIR nlohmann/json.hpp REQUIRED)
file(READ "${NLOHMANN_JSON_INCLUDE_DIR}/nlohmann/detail/abi_macros.hpp" _json_hdr)
if(NOT _json_hdr MATCHES "#define[ \t]+NLOHMANN_JSON_VERSION_MAJOR[ \t]+([0-9]+)")
  message(FATAL_ERROR "nlohmann/json.hpp found but version macros missing")
endif()
set(_json_major "${CMAKE_MATCH_1}")
string(REGEX MATCH "#define[ \t]+NLOHMANN_JSON_VERSION_MINOR[ \t]+([0-9]+)" _ "${_json_hdr}")
set(_json_minor "${CMAKE_MATCH_1}")
string(REGEX MATCH "#define[ \t]+NLOHMANN_JSON_VERSION_PATCH[ \t]+([0-9]+)" _ "${_json_hdr}")
set(_json_patch "${CMAKE_MATCH_1}")
if(NOT (_json_major EQUAL 3 AND _json_minor EQUAL 11 AND _json_patch EQUAL 3))
  message(FATAL_ERROR
    "nlohmann/json 3.11.3 required, found ${_json_major}.${_json_minor}.${_json_patch}")
endif()
message(STATUS "nlohmann/json ${_json_major}.${_json_minor}.${_json_patch} "
               "at ${NLOHMANN_JSON_INCLUDE_DIR}")
# OpenSSL 3: provides EVP SHA-256 + CRYPTO_memcmp
find_package(OpenSSL 3.0 REQUIRED)

find_package(Threads REQUIRED)

add_library(jtp_core STATIC
  src/trajectory_time_parameterization.cpp
  src/io_json.cpp
  src/crypto.cpp
  src/http_server.cpp
)
target_include_directories(jtp_core PUBLIC
  ${CMAKE_CURRENT_SOURCE_DIR}/src
  ${NLOHMANN_JSON_INCLUDE_DIR}
)
target_link_libraries(jtp_core PUBLIC
  Eigen3::Eigen
  OpenSSL::Crypto
  Threads::Threads
)

add_executable(jtp_server src/main.cpp)
target_link_libraries(jtp_server PRIVATE jtp_core)

enable_testing()
add_executable(test_parameterization tests/test_parameterization.cpp)
target_link_libraries(test_parameterization PRIVATE jtp_core)
add_test(NAME unit_tests COMMAND test_parameterization)
add_test(NAME e2e_http
  COMMAND ${CMAKE_CURRENT_SOURCE_DIR}/scripts/e2e_test.sh
          $<TARGET_FILE:jtp_server>
          ${CMAKE_CURRENT_SOURCE_DIR}/examples
)
