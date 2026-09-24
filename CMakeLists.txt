cmake_minimum_required(VERSION 3.16)
project(point_cloud_registration LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

if(NOT CMAKE_BUILD_TYPE)
  set(CMAKE_BUILD_TYPE Release)
endif()

add_compile_options(-Wall -Wextra -Wpedantic)

# Vendored, checksum-pinned Eigen (see third_party/LOCK.txt). No find_package /
# network access is required to build.
set(EIGEN_INCLUDE_DIR "${CMAKE_CURRENT_SOURCE_DIR}/third_party")
if(NOT EXISTS "${EIGEN_INCLUDE_DIR}/Eigen/Dense")
  message(FATAL_ERROR "Vendored Eigen missing. See third_party/LOCK.txt")
endif()

add_library(pcr_core STATIC
  src/icp.cpp
  src/http_server.cpp
  src/api.cpp
)
target_include_directories(pcr_core PUBLIC src ${EIGEN_INCLUDE_DIR})
find_package(Threads REQUIRED)
target_link_libraries(pcr_core PUBLIC Threads::Threads)

add_executable(pcr-server src/main.cpp)
target_link_libraries(pcr-server PRIVATE pcr_core)

enable_testing()
add_executable(test_icp tests/test_icp.cpp)
target_link_libraries(test_icp PRIVATE pcr_core)
add_test(NAME icp COMMAND test_icp)
