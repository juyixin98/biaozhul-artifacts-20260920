cmake_minimum_required(VERSION 3.10)
project(shmrq CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

if(NOT CMAKE_BUILD_TYPE)
  set(CMAKE_BUILD_TYPE Release)
endif()

add_compile_options(-Wall -Wextra -Wpedantic -Wshadow -Wconversion -Wsign-conversion)

add_library(shmrq_core STATIC
  src/ring.cpp
  src/shm.cpp
  src/crc.cpp
  src/futex.cpp
)
target_include_directories(shmrq_core PUBLIC include)
find_package(Threads REQUIRED)
target_link_libraries(shmrq_core PUBLIC Threads::Threads)
include(CheckLibraryExists)
check_library_exists(rt shm_open "" HAVE_LIBRT)
if(HAVE_LIBRT)
  target_link_libraries(shmrq_core PUBLIC rt)
endif()

add_executable(shmrq src/main.cpp)
target_link_libraries(shmrq PRIVATE shmrq_core)

enable_testing()
add_executable(shmrq_unit tests/unit.cpp)
target_link_libraries(shmrq_unit PRIVATE shmrq_core)
add_test(NAME unit COMMAND shmrq_unit)
add_test(NAME integration COMMAND bash ${CMAKE_CURRENT_SOURCE_DIR}/tests/run_tests.sh
         $<TARGET_FILE:shmrq>)
