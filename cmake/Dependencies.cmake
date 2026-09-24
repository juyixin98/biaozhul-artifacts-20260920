# Pinned third-party dependencies (locked by SHA-256).
# The build never invokes a package manager: it downloads (or reuses
# .deps-cache/) these exact files and verifies their hashes.
#
# Mirrored by deps-lock.json — keep the two in sync.

# --- Eigen 3.4.0 (header-only linear algebra; MPL-2.0) ---
set(PF_EIGEN_URL
    "https://gitlab.com/libeigen/eigen/-/archive/3.4.0/eigen-3.4.0.tar.gz"
    CACHE STRING "Override Eigen 3.4.0 tarball URL (mirror/file:// allowed)")
set(PF_EIGEN_SHA256
    "8586084f71f9bde545ee7fa6d00288b264a2b7ac3607b974e54d13e7162c1c72")

# --- nlohmann/json v3.11.3 single header (MIT) ---
set(PF_JSON_URL
    "https://github.com/nlohmann/json/releases/download/v3.11.3/json.hpp"
    CACHE STRING "Override nlohmann/json v3.11.3 URL")
set(PF_JSON_SHA256
    "9bea4c8066ef4a1c206b2be5a36302f8926f7fdc6087af5d20b417d0cf103ea6")

# --- cpp-httplib v0.15.3 single header (MIT) ---
set(PF_HTTPLIB_URL
    "https://raw.githubusercontent.com/yhirose/cpp-httplib/v0.15.3/httplib.h"
    CACHE STRING "Override cpp-httplib v0.15.3 URL")
set(PF_HTTPLIB_SHA256
    "a3347656c71c81c3fe510fdb1ff229d7db52eb7df04d3342d340eba0b6ad50ea")

# Prefer a pre-seeded project-local cache so a fully offline machine works.
set(PF_DEPS_CACHE "${CMAKE_SOURCE_DIR}/.deps-cache"
    CACHE PATH "Directory checked for cached dependency files")

include(${CMAKE_CURRENT_LIST_DIR}/FetchLocked.cmake)

# Vendored include tree (populated at configure time).
set(PF_VENDORED_DIR "${CMAKE_BINARY_DIR}/_vendored" CACHE PATH
    "Directory into which locked dependencies are materialized")
file(MAKE_DIRECTORY "${PF_VENDORED_DIR}/include")

# --- Eigen: extract only the Eigen/ header tree ---
pf_fetch_locked(
    URL "${PF_EIGEN_URL}"
    SHA256 "${PF_EIGEN_SHA256}"
    OUTPUT eigen-3.4.0.tar.gz
    RESULT PF_EIGEN_TGZ)
if(NOT EXISTS "${PF_VENDORED_DIR}/include/Eigen/Core")
    message(STATUS "Extracting Eigen headers ...")
    execute_process(
        COMMAND ${CMAKE_COMMAND} -E tar xzf "${PF_EIGEN_TGZ}"
        WORKING_DIRECTORY "${PF_VENDORED_DIR}"
        RESULT_VARIABLE _rc)
    if(NOT _rc EQUAL 0)
        message(FATAL_ERROR "Failed to extract Eigen tarball")
    endif()
    # Signature header needed for #include <Eigen/*>; move both trees up.
    file(RENAME "${PF_VENDORED_DIR}/eigen-3.4.0/Eigen"
         "${PF_VENDORED_DIR}/include/Eigen")
    file(RENAME "${PF_VENDORED_DIR}/eigen-3.4.0/unsupported"
         "${PF_VENDORED_DIR}/include/unsupported")
endif()

# --- nlohmann/json ---
pf_fetch_locked(
    URL "${PF_JSON_URL}"
    SHA256 "${PF_JSON_SHA256}"
    OUTPUT json.hpp
    RESULT PF_JSON_HPP)
file(COPY_FILE "${PF_JSON_HPP}" "${PF_VENDORED_DIR}/include/json.hpp"
     ONLY_IF_DIFFERENT)

# --- cpp-httplib (compile its inline implementation once) ---
pf_fetch_locked(
    URL "${PF_HTTPLIB_URL}"
    SHA256 "${PF_HTTPLIB_SHA256}"
    OUTPUT httplib.h
    RESULT PF_HTTPLIB_HPP)
file(COPY_FILE "${PF_HTTPLIB_HPP}" "${PF_VENDORED_DIR}/include/httplib.h"
     ONLY_IF_DIFFERENT)

add_library(pf_httplib STATIC ${CMAKE_CURRENT_LIST_DIR}/httplib_tu.cpp)
target_include_directories(pf_httplib SYSTEM PUBLIC
    "${PF_VENDORED_DIR}/include")
# Never define CPPHTTPLIB_OPENSSL_SUPPORT: httplib keys off #ifdef, so even
# =0 enables SSL and pulls in libssl. Plaintext HTTP; auth is HMAC in-app.
find_package(Threads REQUIRED)
target_link_libraries(pf_httplib PUBLIC Threads::Threads)

add_library(pf_deps INTERFACE)
# SYSTEM: consumers must not be forced to be warning-clean for vendored code.
target_include_directories(pf_deps SYSTEM INTERFACE
    "${PF_VENDORED_DIR}/include")
target_link_libraries(pf_deps INTERFACE pf_httplib)

find_package(OpenSSL 3.0 REQUIRED)
target_link_libraries(pf_deps INTERFACE OpenSSL::Crypto)
