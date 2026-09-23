cmake_minimum_required(VERSION 3.16)
project(pgo_se2 LANGUAGES CXX VERSION 1.0.0)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

if(NOT CMAKE_BUILD_TYPE)
  set(CMAKE_BUILD_TYPE Release CACHE STRING "Build type" FORCE)
endif()

add_compile_options(-Wall -Wextra -Wpedantic)

# ---- Locked dependencies (see DEPS.lock for exact verified versions) ----
find_package(Ceres 2.1 REQUIRED)            # 2.2.0 verified
find_package(Eigen3 3.4 REQUIRED)          # 3.4.0 verified
find_package(SQLite3 REQUIRED)             # 3.45.1 verified
find_package(OpenSSL 3.0 REQUIRED)         # 3.0.13 verified
find_package(nlohmann_json 3.9 REQUIRED)   # 3.11.3 verified

set(PGO_SOURCES
  src/canonical.cpp
  src/crypto.cpp
  src/graph.cpp
  src/db.cpp
  src/optimizer.cpp
  src/report.cpp
  src/util.cpp
)

add_executable(pgo src/main.cpp ${PGO_SOURCES})
target_include_directories(pgo PRIVATE src)
target_link_libraries(pgo PRIVATE
  Ceres::ceres
  Eigen3::Eigen
  SQLite::SQLite3
  OpenSSL::Crypto
  nlohmann_json::nlohmann_json
)
target_compile_definitions(pgo PRIVATE
  PGO_VERSION="${PROJECT_VERSION}"
  PGO_CERES_VERSION_STR="${CERES_VERSION}"
)

# ---- Unit tests ----
enable_testing()
add_executable(pgo_tests tests/test_unit.cpp ${PGO_SOURCES})
target_include_directories(pgo_tests PRIVATE src)
target_link_libraries(pgo_tests PRIVATE
  Ceres::ceres
  Eigen3::Eigen
  SQLite::SQLite3
  OpenSSL::Crypto
  nlohmann_json::nlohmann_json
)
target_compile_definitions(pgo_tests PRIVATE
  PGO_VERSION="${PROJECT_VERSION}"
  PGO_CERES_VERSION_STR="${CERES_VERSION}"
)
add_test(NAME unit COMMAND pgo_tests)
