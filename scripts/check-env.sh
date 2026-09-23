#!/usr/bin/env bash
# Verify that the local toolchain satisfies DEPS.lock. Prints one line per
# component and exits non-zero if anything is missing or out of range.
# Never modifies the system; never accesses the network.
set -u

fail=0
ok()   { printf '  [ OK ] %-14s %s\n' "$1" "$2"; }
bad()  { printf '  [FAIL] %-14s %s\n' "$1" "$2"; fail=1; }

ver_ge() { # ver_ge A B  -> A >= B
  [ "$(printf '%s\n%s\n' "$2" "$1" | sort -V | head -n1)" = "$2" ]
}
ver_lt() { [ "$(printf '%s\n%s\n' "$1" "$2" | sort -V | head -n1)" = "$1" ] && [ "$1" != "$2" ]; }

need_range() { # name actual min max
  local name="$1" actual="$2" minv="$3" maxv="$4"
  if [ -z "$actual" ]; then bad "$name" "not found (need >=$minv <$maxv)"; return; fi
  if ver_ge "$actual" "$minv" && ver_lt "$actual" "$maxv"; then
    ok "$name" "$actual (locked >=$minv <$maxv)"
  else
    bad "$name" "$actual outside locked range >=$minv <$maxv"
  fi
}

echo "== PGO-SE2 environment check =="

cmake_v="$(cmake --version 2>/dev/null | awk 'NR==1{print $3}')"
need_range cmake "${cmake_v:-}" 3.16 4.0

gcc_v="$(g++ -dumpfullversion -dumpversion 2>/dev/null)"
need_range gcc "${gcc_v:-}" 10 15

ceres_v="$(awk '/#define CERES_VERSION_MAJOR/{a=$3}
                /#define CERES_VERSION_MINOR/{b=$3}
                /#define CERES_VERSION_PATCH/{c=$3}
                END{if(a!="")printf "%d.%d.%d",a,b,c}' \
          /usr/include/ceres/version.h 2>/dev/null)"
if [ -z "$ceres_v" ]; then
  ceres_v="$(find /usr/include /usr/local/include -maxdepth 3 -path '*/ceres/version.h' 2>/dev/null \
             | head -1 | xargs awk '/CERES_VERSION_MAJOR/{a=$3}/CERES_VERSION_MINOR/{b=$3}/CERES_VERSION_PATCH/{c=$3}END{printf "%d.%d.%d",a,b,c}' 2>/dev/null)"
fi
need_range ceres "${ceres_v:-}" 2.1.0 3.0.0

eigen_v="$(awk '/#define EIGEN_WORLD_VERSION/{a=$3}
                /#define EIGEN_MAJOR_VERSION/{b=$3}
                /#define EIGEN_MINOR_VERSION/{c=$3; exit}
                END{if(a!="")printf "%d.%d.%d",a,b,c}' \
          /usr/include/eigen3/Eigen/src/Core/util/Macros.h 2>/dev/null)"
need_range eigen "${eigen_v:-}" 3.4.0 4.0.0

sqlite_v="$(pkg-config --modversion sqlite3 2>/dev/null)"
need_range sqlite3 "${sqlite_v:-}" 3.30.0 4.0.0

ssl_v="$(pkg-config --modversion openssl 2>/dev/null)"
need_range openssl "${ssl_v:-}" 3.0.0 4.0.0

nloh_hdr="$(ls /usr/include/nlohmann/detail/abi_macros.hpp \
                /usr/local/include/nlohmann/detail/abi_macros.hpp 2>/dev/null | head -1)"
[ -z "$nloh_hdr" ] && nloh_hdr="$(ls /usr/include/nlohmann/json.hpp \
                /usr/local/include/nlohmann/json.hpp 2>/dev/null | head -1)"
nloh_v="$(awk '/#define NLOHMANN_JSON_VERSION_MAJOR/{a=$3}
               /#define NLOHMANN_JSON_VERSION_MINOR/{b=$3}
               /#define NLOHMANN_JSON_VERSION_PATCH/{c=$3;exit}
               END{if(a!="")printf "%d.%d.%d",a,b,c}' \
         "$nloh_hdr" 2>/dev/null)"
need_range nlohmann_json "${nloh_v:-}" 3.9.0 4.0.0

if [ "$fail" -ne 0 ]; then
  echo "Environment does not satisfy DEPS.lock — refusing to build."
  exit 1
fi
echo "All locked dependencies satisfied."
