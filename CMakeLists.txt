cmake_minimum_required(VERSION 3.16)
project(joint_trajectory_topp LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

if(NOT CMAKE_BUILD_TYPE)
  set(CMAKE_BUILD_TYPE Release CACHE STRING "" FORCE)
endif()
add_compile_options(-Wall -Wextra -Wpedantic)

# ---- Dependency: Eigen (header-only), pinned in dependencies.lock ---------
# Preferred: system Eigen (this project is pinned against 3.4.0).
# Fallback : vendored archive under third_party/eigen (see scripts/setup_deps.sh).
set(TOPP_EIGEN_INCLUDE "" CACHE PATH "Directory containing Eigen/Dense")
if(TOPP_EIGEN_INCLUDE)
  message(STATUS "Eigen: using -DTOPP_EIGEN_INCLUDE=${TOPP_EIGEN_INCLUDE}")
  set(EIGEN3_INCLUDE_DIR "${TOPP_EIGEN_INCLUDE}")
elseif(EXISTS "${CMAKE_SOURCE_DIR}/third_party/eigen/Eigen/Dense")
  set(EIGEN3_INCLUDE_DIR "${CMAKE_SOURCE_DIR}/third_party/eigen")
  message(STATUS "Eigen: using vendored copy in third_party/eigen")
else()
  find_package(Eigen3 3.4 EXACT QUIET NO_MODULE)
  if(NOT Eigen3_FOUND)
    find_package(Eigen3 3.3 REQUIRED NO_MODULE)
  endif()
  message(STATUS "Eigen: system ${EIGEN3_VERSION_STRING}")
endif()

add_library(topp_core STATIC
  src/json.cpp
  src/spline.cpp
  src/parameterize.cpp
  src/planner.cpp
  src/api.cpp
  src/http_server.cpp
)
target_include_directories(topp_core PUBLIC
  ${CMAKE_SOURCE_DIR}/include
  ${EIGEN3_INCLUDE_DIR}
)
if(TARGET Eigen3::Eigen)
  target_link_libraries(topp_core PUBLIC Eigen3::Eigen)
endif()
find_package(Threads REQUIRED)
target_link_libraries(topp_core PUBLIC Threads::Threads)

add_executable(topp_server src/server_main.cpp)
target_link_libraries(topp_server PRIVATE topp_core)

add_executable(topp_cli src/cli_main.cpp)
target_link_libraries(topp_cli PRIVATE topp_core)

enable_testing()
add_executable(topp_tests tests/test_main.cpp)
target_link_libraries(topp_tests PRIVATE topp_core)
add_test(NAME unit_tests COMMAND topp_tests)
