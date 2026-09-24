// SPDX-License-Identifier: Apache-2.0
//
// Immutable configuration snapshot and the persistent atomic store.
//
// This layer deliberately has NO rclcpp dependency: it is plain C++17 over
// SQLite + OpenSSL so that the consistency, persistence and recovery logic can
// be unit-tested without a ROS executor.
#pragma once

#include <cstdint>
#include <functional>
#include <memory>
#include <mutex>
#include <string>
#include <vector>

struct sqlite3;

namespace robot_param_atomic
{

// One complete, self-consistent configuration. Callbacks always receive a
// shared_ptr to an immutable snapshot of this type, so a callback can never
// observe a partially updated field set.
struct Snapshot
{
  double sampling_rate_hz = 0.0;
  double cache_length_s = 0.0;
  double allowed_latency_s = 0.0;

  bool operator==(const Snapshot & other) const;
  bool operator!=(const Snapshot & other) const { return !(*this == other); }
};

// Outcome of attempting a transactional update.
enum class CommitCode : uint8_t
{
  OK = 0,
  REJECT_CONSTRAINT = 1,          // per-field range or cross-field invariant
  REJECT_STALE_VERSION = 2,       // expected_version did not match
  REJECT_UPDATE_IN_CALLBACK = 3,  // re-entrant commit from a callback
  ERR_PERSISTENCE = 4,            // SQLite write failed; transaction rolled back
  ERR_INTERNAL = 5,
};

struct CommitResult
{
  CommitCode code = CommitCode::ERR_INTERNAL;
  std::string message;
  // Snapshot+version AFTER the attempt (unchanged when rejected).
  double version = 0.0;
  Snapshot snapshot;
  bool ok() const { return code == CommitCode::OK; }
};

// How startup loaded (or failed to load) the persisted configuration.
enum class LoadStatus : uint8_t
{
  FRESH = 0,            // no database / no row: defaults used
  LOADED = 1,           // latest committed row validated and loaded
  CORRUPT_REJECTED = 2, // latest row failed hash/schema validation; fell back
};

struct LoadOutcome
{
  LoadStatus status = LoadStatus::FRESH;
  std::string message;
  double version = 0.0;
  Snapshot snapshot;
  std::string hash;
  int64_t commit_seq = 0;
};

// Result of validating a candidate snapshot.
struct Validation
{
  bool valid = true;
  std::string reason;  // human-readable, names the offending field(s)
  explicit operator bool() const { return valid; }
};

// Canonical JSON serialisation of a snapshot+version. Byte-stable across
// processes (fixed key order, %.17g for doubles); its SHA-256 is the
// integrity hash stored beside the row.
std::string to_canonical_json(const Snapshot & s, double version);

// Validate fields individually and together. Public so tests and the ROS node
// can give the same error text.
//   sampling_rate_hz > 0 (finite)
//   cache_length_s  >= 0 (finite)
//   allowed_latency_s >= 0 (finite)
//   cache_length_s >= 2 * allowed_latency_s
Validation validate(const Snapshot & candidate);

// Change callback. Invoked synchronously, inside the store mutex, AFTER a
// successful commit. Receives a fully formed immutable snapshot (old -> new).
// A callback MUST NOT call commit(): re-entrant commits are rejected with
// REJECT_UPDATE_IN_CALLBACK.
using ChangeCallback =
  std::function<void(const Snapshot & old_snap, const Snapshot & new_snap, double new_version)>;

class ConfigStore
{
public:
  ConfigStore();
  ~ConfigStore();

  ConfigStore(const ConfigStore &) = delete;
  ConfigStore & operator=(const ConfigStore &) = delete;

  // Open/create the SQLite database and load the latest committed snapshot.
  // Never throws for a corrupt record: a failing latest row is reported with
  // LoadStatus::CORRUPT_REJECTED and the store falls back to defaults while
  // preserving the on-disk history for inspection.
  LoadOutcome open(const std::string & db_path, const Snapshot & defaults);

  // Register a callback fired after every successful commit. Must be called
  // before commits (typically right after open). Callbacks run under the
  // store mutex; keep them short and non-blocking.
  void set_callback(ChangeCallback cb);

  // Atomic compare-and-swap update.
  //   1. reject if currently executing a callback (re-entrancy guard)
  //   2. reject unless expected_version == current version
  //   3. merge the candidate onto the current snapshot and validate the WHOLE
  //   4. BEGIN IMMEDIATE, insert new row with version+1, commit SQLite txn
  // On ANY persistence failure the SQLite transaction is rolled back and the
  // in-memory state is left untouched -> all-or-nothing.
  CommitResult commit(
    double expected_version,
    const Snapshot & candidate,
    bool partial = false);  // true: any field == KEEP means "unchanged"

  // Accessors for the current committed state.
  Snapshot current_snapshot() const;
  double current_version() const;
  std::string current_hash() const;
  int64_t commit_seq() const;

  // A field equal to this constant in a partial candidate means "no change".
  static constexpr double KEEP = -1.0;

private:
  // Recursive because a change callback is invoked while this mutex is held on
  // the committing thread; a re-entrant commit() must be able to lock it
  // again in order to be rejected cleanly (rather than dead-locking). Other
  // threads remain blocked for the whole callback, serialising snapshots.
  mutable std::recursive_mutex mutex_;
  sqlite3 * db_ = nullptr;
  Snapshot current_;
  double version_ = 0.0;
  std::string hash_;
  int64_t commit_seq_ = 0;
  ChangeCallback callback_;

  // Helpers (all called with mutex_ held).
  CommitResult commit_locked(double expected_version, Snapshot candidate, bool partial);
  bool insert_row(double version, const Snapshot & s, std::string & error_out);
};

}  // namespace robot_param_atomic
