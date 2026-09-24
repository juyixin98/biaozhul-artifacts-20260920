cmake_minimum_required(VERSION 3.16)
project(gridfusion CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

if(NOT CMAKE_BUILD_TYPE)
  set(CMAKE_BUILD_TYPE Release)
endif()

add_compile_options(-Wall -Wextra -Wpedantic)

find_package(Eigen3 3.3 REQUIRED NO_MODULE)

# ---------------------------------------------------------------------------
# Verify pinned vendored dependencies (lock: third_party/LOCK)
# ---------------------------------------------------------------------------
file(SHA256 ${CMAKE_CURRENT_SOURCE_DIR}/third_party/httplib.h
     GRIDFUSION_HTTPLIB_SHA)
file(SHA256 ${CMAKE_CURRENT_SOURCE_DIR}/third_party/nlohmann/json.hpp
     GRIDFUSION_JSON_SHA)
set(GRIDFUSION_EXPECTED_HTTPLIB
    "a0a0c13dc086663863dbe6730a19716f7d3744904b6205496e8301750814dbd5")
set(GRIDFUSION_EXPECTED_JSON
    "9bea4c8066ef4a1c206b2be5a36302f8926f7fdc6087af5d20b417d0cf103ea6")
if(NOT GRIDFUSION_HTTPLIB_SHA STREQUAL GRIDFUSION_EXPECTED_HTTPLIB)
  message(FATAL_ERROR
    "third_party/httplib.h checksum mismatch (lock violation).\n"
    "  expected ${GRIDFUSION_EXPECTED_HTTPLIB}\n"
    "  got      ${GRIDFUSION_HTTPLIB_SHA}")
endif()
if(NOT GRIDFUSION_JSON_SHA STREQUAL GRIDFUSION_EXPECTED_JSON)
  message(FATAL_ERROR
    "third_party/nlohmann/json.hpp checksum mismatch (lock violation).\n"
    "  expected ${GRIDFUSION_EXPECTED_JSON}\n"
    "  got      ${GRIDFUSION_JSON_SHA}")
endif()

# ---------------------------------------------------------------------------
add_library(gridfusion_core STATIC
  src/sha256.cpp
  src/raycast.cpp
  src/grid_map.cpp
  src/protocol.cpp
  src/server.cpp)
target_include_directories(gridfusion_core PUBLIC
  ${CMAKE_CURRENT_SOURCE_DIR}/include
  ${CMAKE_CURRENT_SOURCE_DIR}/third_party)
target_link_libraries(gridfusion_core PUBLIC Eigen3::Eigen)
find_package(Threads REQUIRED)
target_link_libraries(gridfusion_core PUBLIC Threads::Threads)

add_executable(gridfusion-server src/server_main.cpp)
target_link_libraries(gridfusion-server PRIVATE gridfusion_core)

add_executable(gridfusion src/cli_main.cpp)
target_link_libraries(gridfusion PRIVATE gridfusion_core)

# ---------------------------------------------------------------------------
enable_testing()
set(GF_TESTS test_sha256 test_grid test_http)
foreach(t ${GF_TESTS})
  add_executable(${t} tests/${t}.cpp)
  target_link_libraries(${t} PRIVATE gridfusion_core)
  add_test(NAME ${t} COMMAND ${t})
  set_tests_properties(${t} PROPERTIES TIMEOUT 60)
endforeach()
