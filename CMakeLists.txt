cmake_minimum_required(VERSION 3.16)
project(point_cloud_registration LANGUAGES CXX)

# ---- Locked toolchain -------------------------------------------------------
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

if(NOT CMAKE_BUILD_TYPE)
    set(CMAKE_BUILD_TYPE Release CACHE STRING "Build type" FORCE)
endif()
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)

add_compile_options(-Wall -Wextra -Wpedantic)

find_package(Threads REQUIRED)

# ---- Locked dependency: Eigen 3.4.0 ----------------------------------------
# The system package is pinned by Debian/Ubuntu to exactly 3.4.0; the find
# call below requires >= 3.4.0 and rejects 4.x APIs accidentally appearing.
find_package(Eigen3 3.4.0 EXACT REQUIRED NO_MODULE)

# ---- Vendored dependency: nlohmann/json v3.11.3 (single header) -------------
# Checksum is verified at configure time so the vendored copy cannot silently
# change. Re-download with: scripts/fetch_deps.sh
set(JSON_HEADER "${CMAKE_CURRENT_SOURCE_DIR}/third_party/nlohmann/json.hpp")
set(JSON_EXPECTED_SHA256
    "d6c65faca48676c2ed4592e3aa444e5256368177a3b8dcf422e2a43581cf87a0")
if(NOT EXISTS "${JSON_HEADER}")
    message(FATAL_ERROR
        "Vendored nlohmann/json.hpp missing. Run: scripts/fetch_deps.sh")
endif()
file(SHA256 "${JSON_HEADER}" JSON_ACTUAL_SHA256)
if(NOT JSON_ACTUAL_SHA256 STREQUAL JSON_EXPECTED_SHA256)
    message(FATAL_ERROR
        "third_party/nlohmann/json.hpp checksum mismatch.\n"
        "  expected ${JSON_EXPECTED_SHA256}\n"
        "  actual   ${JSON_ACTUAL_SHA256}\n"
        "Re-run scripts/fetch_deps.sh.")
endif()

add_library(nlohmann_json INTERFACE)
target_include_directories(nlohmann_json INTERFACE third_party)

# ---- Core library -----------------------------------------------------------
add_library(pcrs_core STATIC
    src/icp.cpp
    src/sha256.cpp
    src/protocol.cpp
    src/server.cpp)
target_include_directories(pcrs_core PUBLIC src)
target_link_libraries(pcrs_core PUBLIC Eigen3::Eigen nlohmann_json Threads::Threads)

# ---- Server binary ----------------------------------------------------------
add_executable(pcrs_server src/main.cpp)
target_link_libraries(pcrs_server PRIVATE pcrs_core)

# ---- Tests ------------------------------------------------------------------
enable_testing()
add_executable(test_icp tests/test_icp.cpp)
target_link_libraries(test_icp PRIVATE pcrs_core)
add_test(NAME icp_unit_tests COMMAND test_icp)

add_executable(test_sha256 tests/test_sha256.cpp)
target_link_libraries(test_sha256 PRIVATE pcrs_core)
add_test(NAME sha256_kat COMMAND test_sha256)

# ---- Example generator ------------------------------------------------------
add_executable(gen_examples tools/gen_examples.cpp)
target_link_libraries(gen_examples PRIVATE nlohmann_json Eigen3::Eigen)
