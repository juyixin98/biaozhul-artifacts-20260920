cmake_minimum_required(VERSION 3.16)
project(grid_prob_fusion
    VERSION 1.0.0
    DESCRIPTION "Offline 2D occupancy-grid log-odds probability fusion service"
    LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

if(NOT CMAKE_BUILD_TYPE)
    set(CMAKE_BUILD_TYPE Release CACHE STRING "Build type" FORCE)
endif()
add_compile_options(-Wall -Wextra -Wpedantic)

enable_testing()
include(cmake/Dependencies.cmake)

add_library(pf_core STATIC
    src/crypto.cpp
    src/grid.cpp
    src/fusion.cpp
    src/service.cpp
    src/protocol.cpp
    src/http_server.cpp
)
target_include_directories(pf_core PUBLIC src)
target_link_libraries(pf_core PUBLIC pf_deps)

add_executable(pgrid src/cli_main.cpp)
target_link_libraries(pgrid PRIVATE pf_core)

# --- unit / integration tests ---
add_executable(test_math tests/test_math.cpp)
target_link_libraries(test_math PRIVATE pf_core)
add_test(NAME math COMMAND test_math)

add_executable(test_raycast tests/test_raycast.cpp)
target_link_libraries(test_raycast PRIVATE pf_core)
add_test(NAME raycast COMMAND test_raycast)

add_executable(test_fusion tests/test_fusion.cpp)
target_link_libraries(test_fusion PRIVATE pf_core)
add_test(NAME fusion COMMAND test_fusion)

add_executable(test_crypto tests/test_crypto.cpp)
target_link_libraries(test_crypto PRIVATE pf_core)
add_test(NAME crypto COMMAND test_crypto)

add_executable(test_http tests/test_http.cpp)
target_link_libraries(test_http PRIVATE pf_core)
add_test(NAME http COMMAND test_http)

add_executable(test_scenarios tests/test_scenarios.cpp)
target_link_libraries(test_scenarios PRIVATE pf_core)
add_test(NAME scenarios COMMAND test_scenarios)
