# pf_fetch_locked(URL <url> SHA256 <hex> OUTPUT <name> RESULT <var>)
#
# Resolves a dependency file to a verified local path:
#   1. If ${PF_DEPS_CACHE}/<OUTPUT> exists and its SHA-256 matches, use it.
#   2. Otherwise download URL to <OUTPUT>, then verify SHA-256.
# A hash mismatch aborts configure — a tampered/wrong dependency is never used.

include_guard(GLOBAL)

function(pf_fetch_locked)
    cmake_parse_arguments(ARG "" "URL;SHA256;OUTPUT;RESULT" "" ${ARGN})
    if(NOT ARG_URL OR NOT ARG_SHA256 OR NOT ARG_OUTPUT OR NOT ARG_RESULT)
        message(FATAL_ERROR "pf_fetch_locked requires URL/SHA256/OUTPUT/RESULT")
    endif()

    set(cached "${PF_DEPS_CACHE}/${ARG_OUTPUT}")
    if(EXISTS "${cached}")
        file(SHA256 "${cached}" actual_sha)
        if(actual_sha STREQUAL ARG_SHA256)
            message(STATUS "Using cached verified dependency: ${cached}")
            set(${ARG_RESULT} "${cached}" PARENT_SCOPE)
            return()
        endif()
        message(WARNING "Cached ${ARG_OUTPUT} SHA-256 mismatch; re-downloading")
    endif()

    set(dest "${PF_VENDORED_DIR}/staging/${ARG_OUTPUT}")
    get_filename_component(dest_dir "${dest}" DIRECTORY)
    file(MAKE_DIRECTORY "${dest_dir}")
    message(STATUS "Downloading ${ARG_URL}")
    file(DOWNLOAD "${ARG_URL}" "${dest}"
         TLS_VERIFY ON
         SHOW_PROGRESS
         STATUS dl_status)
    list(GET dl_status 0 dl_code)
    list(GET dl_status 1 dl_msg)
    if(NOT dl_code EQUAL 0)
        message(FATAL_ERROR
            "Download failed for ${ARG_URL}: ${dl_msg}\n"
            "Pre-seed ${PF_DEPS_CACHE}/${ARG_OUTPUT} for an offline build.")
    endif()

    file(SHA256 "${dest}" actual_sha)
    if(NOT actual_sha STREQUAL ARG_SHA256)
        file(REMOVE "${dest}")
        message(FATAL_ERROR
            "SHA-256 mismatch for ${ARG_OUTPUT}\n"
            "  expected ${ARG_SHA256}\n"
            "  actual   ${actual_sha}")
    endif()

    # Populate the cache so subsequent configure runs are offline.
    file(COPY_FILE "${dest}" "${cached}" ONLY_IF_DIFFERENT)
    set(${ARG_RESULT} "${dest}" PARENT_SCOPE)
endfunction()
