// SPDX-License-Identifier: Apache-2.0
#pragma once

#include <cstdint>
#include <memory>
#include <mutex>
#include <string>
#include <vector>

#include "robot_param/core/crypto.hpp"
#include "robot_param/core/types.hpp"

struct sqlite3;

namespace robot_param {

// What happened when the database was opened at startup.
enum class LoadState : std::uint8_t {
  FRESH = 0,            // database existed but contained no revisions
  LOADED = 1,           // newest revision verified and loaded
  RECOVERED = 2,        // newest revision(s) corrupt; fell back to an older good one
  NO_VALID_CONFIG = 3,  // revisions exist but every single one is corrupt
};

struct LoadInfo {
  LoadState state = LoadState::FRESH;
  std::string detail;  // precise, human-readable corruption/fallback report
};

// Status codes shared with the ROS service (values must stay in sync).
enum class CommitStatus : std::uint8_t {
  OK = 0,
  REJECTED_VALIDATION = 1,
  VERSION_CONFLICT = 2,
  PERSISTENCE_FAILED = 3,
};

struct CommitResult {
  CommitStatus status = CommitStatus::OK;
  std::string message;
  ConfigSnapshot snapshot;  // committed snapshot on success, otherwise unchanged current
};

// SQLite-backed, append-only revision store.
//
// Every committed revision is one INSERT inside an immediate transaction.
// Each row carries a SHA-256 of its canonical payload (tamper/bit-rot
// detection) and an HMAC-SHA-256 under the node key (authenticity: a row
// written by something else is rejected even if internally consistent).
//
// Thread-safe: commits are serialized by an internal mutex, so concurrent
// service callbacks can never interleave validation and persistence.
class ConfigStore {
 public:
  // Opens/creates the database. Throws std::runtime_error only for
  // unrecoverable open problems (missing directory permissions, file that is
  // not a SQLite database, structural corruption reported by PRAGMA
  // quick_check). Tampered ROWS are not thrown: they are reported through
  // LoadInfo so the node can start in RECOVERED/NO_VALID_CONFIG state.
  static std::unique_ptr<ConfigStore> open(const std::string& db_path,
                                           std::vector<std::uint8_t> key);

  ~ConfigStore();

  ConfigStore(const ConfigStore&) = delete;
  ConfigStore& operator=(const ConfigStore&) = delete;

  // Atomically: check expected version, validate the merged snapshot, persist
  // a new revision, update the in-memory current snapshot. On any failure the
  // store is left exactly as before (transaction rolled back, current
  // untouched).
  CommitResult commit(std::uint64_t expected_version, const ConfigPatch& patch,
                      std::int64_t now_ms);

  // The current committed snapshot (v0 built-in defaults before the first
  // commit). Always safe to read concurrently with commit().
  ConfigSnapshot snapshot() const;
  LoadInfo load_info() const;

  // Canonical byte payload that gets hashed. Exposed for tests/tooling;
  // %.17g formatting makes it identical for any IEEE-754 round-trip.
  static std::string canonical_payload(const ConfigSnapshot& s);

 private:
  ConfigStore() = default;

  struct Stmt;
  void initialize();       // pragmas + schema + quick_check + reload
  void reload_from_disk();  // scan all rows, verify, pick latest good

  std::string db_path_;
  std::vector<std::uint8_t> key_;
  sqlite3* db_ = nullptr;
  mutable std::mutex mutex_;
  ConfigSnapshot current_;  // guarded by mutex_
  LoadInfo load_info_;      // set once in initialize()
  std::uint64_t next_version_ = 1;  // max(row version)+1, guarded by mutex_
};

}  // namespace robot_param
