cmake_minimum_required(VERSION 3.16)
project(pose_graph_optimizer LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)

if(NOT CMAKE_BUILD_TYPE)
  set(CMAKE_BUILD_TYPE Release CACHE STRING "Build type" FORCE)
endif()

# --- 锁定依赖：版本在 configure 阶段强制校验，精确版本记录于 deps.lock ---
find_package(Ceres 2.1 REQUIRED CONFIG)
find_package(Eigen3 3.3 REQUIRED NO_MODULE)
find_package(SQLite3 3.31 REQUIRED)

if(NOT TARGET Ceres::ceres)
  message(FATAL_ERROR "Ceres::ceres target missing, unsupported Ceres package")
endif()

add_library(pgo_core STATIC
  src/json.cpp
  src/sha256.cpp
  src/graph.cpp
  src/io.cpp
  src/db.cpp
  src/optimizer.cpp
  src/signal.cpp
)
target_include_directories(pgo_core PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/src)
target_link_libraries(pgo_core PUBLIC Ceres::ceres Eigen3::Eigen SQLite::SQLite3)
target_compile_options(pgo_core PRIVATE -Wall -Wextra -Wpedantic)

add_executable(pgo src/main.cpp)
target_link_libraries(pgo PRIVATE pgo_core)
target_compile_options(pgo PRIVATE -Wall -Wextra)

add_executable(pgo_gen tools/pgo_gen.cpp)
target_link_libraries(pgo_gen PRIVATE pgo_core)
target_compile_options(pgo_gen PRIVATE -Wall -Wextra)

add_executable(pgo_sql tools/pgo_sql.cpp)
target_link_libraries(pgo_sql PRIVATE SQLite::SQLite3)
target_compile_options(pgo_sql PRIVATE -Wall -Wextra)

enable_testing()

add_executable(pgo_tests tests/test_main.cpp)
target_link_libraries(pgo_tests PRIVATE pgo_core)
target_compile_options(pgo_tests PRIVATE -Wall -Wextra)
add_test(NAME unit COMMAND pgo_tests)

add_test(
  NAME acceptance
  COMMAND bash ${CMAKE_CURRENT_SOURCE_DIR}/tests/acceptance.sh
          $<TARGET_FILE:pgo> $<TARGET_FILE:pgo_gen> ${CMAKE_CURRENT_SOURCE_DIR}
)
