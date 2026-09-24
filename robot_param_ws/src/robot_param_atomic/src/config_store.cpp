// SPDX-License-Identifier: Apache-2.0

#include "robot_param_atomic/config_store.hpp"

#include <sqlite3.h>
#include <openssl/evp.h>

#include <cerrno>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <iomanip>
#include <sstream>
#include <stdexcept>

namespace robot_param_atomic
{

namespace
{

// %.17g round-trips every IEEE-754 double exactly and is deterministic.
std::string fmt_num(double v)
{
  std::ostringstream os;
  os << std::setprecision(17) << v;
  return os.str();
}

std::string sha256_hex(const std::string & bytes)
{
  unsigned char digest[EVP_MAX_MD_SIZE];
  size_t digest_len = 0;
  if (
    EVP_Q_digest(
      nullptr, "SHA256", nullptr, bytes.data(), bytes.size(), digest,
      &digest_len) == 0 ||
    digest_len == 0)
  {
    throw std::runtime_error("SHA-256 computation failed");
  }
  static const char * hex = "0123456789abcdef";
  std::string out;
  out.reserve(digest_len * 2);
  for (size_t i = 0; i < digest_len; ++i) {
    out.push_back(hex[digest[i] >> 4]);
    out.push_back(hex[digest[i] & 0x0f]);
  }
  return out;
}

void exec_checked(sqlite3 * db, const char * sql)
{
  char * err = nullptr;
  int rc = sqlite3_exec(db, sql, nullptr, nullptr, &err);
  if (rc != SQLITE_OK) {
    std::string msg = err ? err : "unknown sqlite error";
    sqlite3_free(err);
    throw std::runtime_error("sqlite exec failed: " + msg);
  }
}

}  // namespace

std::string to_canonical_json(const Snapshot & s, double version)
{
  std::ostringstream os;
  os << '{'
     << "\"version\":" << fmt_num(version)
     << ",\"sampling_rate_hz\":" << fmt_num(s.sampling_rate_hz)
     << ",\"cache_length_s\":" << fmt_num(s.cache_length_s)
     << ",\"allowed_latency_s\":" << fmt_num(s.allowed_latency_s)
     << '}';
  return os.str();
}

bool Snapshot::operator==(const Snapshot & other) const
{
  // NaN-aware equality (canonical JSON would reject NaN before comparison in
  // practice, but keep this total).
  auto eq = [](double a, double b) {
    if (std::isnan(a) && std::isnan(b)) return true;
    return a == b;
  };
  return eq(sampling_rate_hz, other.sampling_rate_hz) &&
         eq(cache_length_s, other.cache_length_s) &&
         eq(allowed_latency_s, other.allowed_latency_s);
}

Validation validate(const Snapshot & c)
{
  auto finite = [](double v) { return std::isfinite(v); };

  if (!finite(c.sampling_rate_hz) || c.sampling_rate_hz <= 0.0) {
    return {false,
            "sampling_rate_hz must be a finite number > 0 (got " +
              fmt_num(c.sampling_rate_hz) + ")"};
  }
  if (!finite(c.cache_length_s) || c.cache_length_s < 0.0) {
    return {false,
            "cache_length_s must be a finite number >= 0 (got " +
              fmt_num(c.cache_length_s) + ")"};
  }
  if (!finite(c.allowed_latency_s) || c.allowed_latency_s < 0.0) {
    return {false,
            "allowed_latency_s must be a finite number >= 0 (got " +
              fmt_num(c.allowed_latency_s) + ")"};
  }
  // Cross-field invariant: the cache must cover at least twice the tolerated
  // latency. Evaluated on the COMPLETE candidate snapshot, never per-field.
  const double required = 2.0 * c.allowed_latency_s;
  if (c.cache_length_s < required) {
    std::ostringstream os;
    os << "cross-field constraint violated: cache_length_s ("
       << fmt_num(c.cache_length_s) << ") must be >= 2 * allowed_latency_s ("
       << fmt_num(required) << ")";
    return {false, os.str()};
  }
  return {true, ""};
}

ConfigStore::ConfigStore() = default;

ConfigStore::~ConfigStore()
{
  if (db_ != nullptr) {
    sqlite3_close(db_);
    db_ = nullptr;
  }
}

void ConfigStore::set_callback(ChangeCallback cb)
{
  std::lock_guard<std::recursive_mutex> lock(mutex_);
  callback_ = std::move(cb);
}

Snapshot ConfigStore::current_snapshot() const
{
  std::lock_guard<std::recursive_mutex> lock(mutex_);
  return current_;
}

double ConfigStore::current_version() const
{
  std::lock_guard<std::recursive_mutex> lock(mutex_);
  return version_;
}

std::string ConfigStore::current_hash() const
{
  std::lock_guard<std::recursive_mutex> lock(mutex_);
  return hash_;
}

int64_t ConfigStore::commit_seq() const
{
  std::lock_guard<std::recursive_mutex> lock(mutex_);
  return commit_seq_;
}

// ---------------------------------------------------------------------------
// open / recovery
// ---------------------------------------------------------------------------

LoadOutcome ConfigStore::open(const std::string & db_path, const Snapshot & defaults)
{
  std::lock_guard<std::recursive_mutex> lock(mutex_);

  if (db_ != nullptr) {
    sqlite3_close(db_);
    db_ = nullptr;
  }

  // Validate the hard-coded defaults themselves; a bad default is a
  // programming error, not a corrupt database.
  if (!validate(defaults)) {
    throw std::invalid_argument("default configuration fails validation");
  }

  sqlite3 * db = nullptr;
  int rc = sqlite3_open_v2(
    db_path.c_str(), &db,
    SQLITE_OPEN_READWRITE | SQLITE_OPEN_CREATE, nullptr);
  if (rc != SQLITE_OK) {
    std::string err = db ? sqlite3_errmsg(db) : strerror(errno);
    if (db) {
      sqlite3_close(db);
    }
    throw std::runtime_error("cannot open config database '" + db_path +
                             "': " + err);
  }
  // Surface precise codes (e.g. SQLITE_READONLY_DIRECTORY / _CANTCONVERT) in
  // error messages instead of the generic primary code.
  sqlite3_extended_result_codes(db, 1);

  // Durable, conservative settings: full sync and rollback journal.
  exec_checked(db, "PRAGMA journal_mode=DELETE;");
  exec_checked(db, "PRAGMA synchronous=FULL;");
  exec_checked(db, "PRAGMA foreign_keys=ON;");

  exec_checked(
    db,
    "CREATE TABLE IF NOT EXISTS config_commits ("
    "  id INTEGER PRIMARY KEY,"
    "  version REAL NOT NULL,"
    "  sampling_rate_hz REAL NOT NULL,"
    "  cache_length_s REAL NOT NULL,"
    "  allowed_latency_s REAL NOT NULL,"
    "  hash TEXT NOT NULL,"
    "  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))"
    ");");

  db_ = db;

  // Read every committed row, newest first. Rows are append-only; each row's
  // hash binds version+payload so tampering is detected on load.
  struct Row
  {
    int64_t id;
    double version;
    Snapshot snap;
    std::string hash;
  };
  std::vector<Row> rows;

  {
    const char * sql =
      "SELECT id, version, sampling_rate_hz, cache_length_s, "
      "allowed_latency_s, hash FROM config_commits ORDER BY id DESC;";
    sqlite3_stmt * stmt = nullptr;
    if (sqlite3_prepare_v2(db, sql, -1, &stmt, nullptr) != SQLITE_OK) {
      throw std::runtime_error(std::string("prepare select failed: ") +
                               sqlite3_errmsg(db));
    }
    while (sqlite3_step(stmt) == SQLITE_ROW) {
      Row r;
      r.id = sqlite3_column_int64(stmt, 0);
      r.version = sqlite3_column_double(stmt, 1);
      r.snap.sampling_rate_hz = sqlite3_column_double(stmt, 2);
      r.snap.cache_length_s = sqlite3_column_double(stmt, 3);
      r.snap.allowed_latency_s = sqlite3_column_double(stmt, 4);
      const unsigned char * stored_hash = sqlite3_column_text(stmt, 5);
      r.hash = stored_hash ? reinterpret_cast<const char *>(stored_hash) : "";
      rows.push_back(r);
    }
    sqlite3_finalize(stmt);
  }

  LoadOutcome out;
  current_ = defaults;
  version_ = 0.0;
  commit_seq_ = 0;
  hash_ = sha256_hex(to_canonical_json(defaults, 0.0));

  if (rows.empty()) {
    out.status = LoadStatus::FRESH;
    out.message = "no committed configuration found in '" + db_path +
                  "'; using built-in defaults";
    out.version = 0.0;
    out.snapshot = defaults;
    out.hash = hash_;
    out.commit_seq = 0;
    return out;
  }

  // Newest-first: find the most recent row we can prove intact & valid.
  bool corrupt_seen = false;
  std::string corrupt_detail;
  for (const Row & r : rows) {
    const std::string canonical = to_canonical_json(r.snap, r.version);
    const std::string recomputed = sha256_hex(canonical);
    Validation v = validate(r.snap);
    if (recomputed != r.hash) {
      corrupt_seen = true;
      corrupt_detail =
        "row id=" + std::to_string(r.id) + " version=" + fmt_num(r.version) +
        " rejected: SHA-256 mismatch (stored=" + r.hash +
        ", recomputed=" + recomputed + ")";
      continue;
    }
    if (!v) {
      corrupt_seen = true;
      corrupt_detail =
        "row id=" + std::to_string(r.id) + " version=" + fmt_num(r.version) +
        " rejected: " + v.reason;
      continue;
    }

    // First (newest) good row.
    current_ = r.snap;
    version_ = r.version;
    commit_seq_ = r.id;
    hash_ = r.hash;
    out.version = r.version;
    out.snapshot = r.snap;
    out.hash = r.hash;
    out.commit_seq = r.id;
    if (corrupt_seen) {
      out.status = LoadStatus::CORRUPT_REJECTED;
      out.message =
        "one or more newer records were corrupt; latest good record loaded. " +
        corrupt_detail;
    } else {
      out.status = LoadStatus::LOADED;
      out.message = "loaded committed configuration version=" +
                    fmt_num(r.version) + " (row id=" +
                    std::to_string(r.id) + ") from '" + db_path + "'";
    }
    return out;
  }

  // Every persisted row failed validation/hash: keep defaults, report clearly.
  out.status = LoadStatus::CORRUPT_REJECTED;
  out.message =
    "all " + std::to_string(rows.size()) +
    " persisted record(s) were corrupt or invalid; built-in defaults used. " +
    corrupt_detail;
  out.version = 0.0;
  out.snapshot = defaults;
  out.hash = hash_;
  out.commit_seq = 0;
  return out;
}

// ---------------------------------------------------------------------------
// commit
// ---------------------------------------------------------------------------

bool ConfigStore::insert_row(
  double version, const Snapshot & s, std::string & error_out)
{
  // Fault-injection hook used ONLY by tests. Forces a failure at the storage
  // layer so the all-or-nothing rollback path can be exercised deterministically.
  if (const char * fault = std::getenv("PARAM_ATOMIC_FAULT")) {
    if (std::strcmp(fault, "commit_io") == 0) {
      error_out =
        "injected persistence fault (PARAM_ATOMIC_FAULT=commit_io)";
      return false;
    }
  }

  const std::string canonical = to_canonical_json(s, version);
  const std::string hash = sha256_hex(canonical);

  int rc = sqlite3_exec(db_, "BEGIN IMMEDIATE;", nullptr, nullptr, nullptr);
  if (rc != SQLITE_OK) {
    error_out = std::string("BEGIN failed: ") + sqlite3_errmsg(db_);
    return false;
  }

  sqlite3_stmt * stmt = nullptr;
  const char * sql =
    "INSERT INTO config_commits "
    "(version, sampling_rate_hz, cache_length_s, allowed_latency_s, hash) "
    "VALUES (?1, ?2, ?3, ?4, ?5);";
  rc = sqlite3_prepare_v2(db_, sql, -1, &stmt, nullptr);
  if (rc != SQLITE_OK) {
    error_out = std::string("prepare insert failed: ") + sqlite3_errmsg(db_);
    sqlite3_exec(db_, "ROLLBACK;", nullptr, nullptr, nullptr);
    return false;
  }
  sqlite3_bind_double(stmt, 1, version);
  sqlite3_bind_double(stmt, 2, s.sampling_rate_hz);
  sqlite3_bind_double(stmt, 3, s.cache_length_s);
  sqlite3_bind_double(stmt, 4, s.allowed_latency_s);
  sqlite3_bind_text(stmt, 5, hash.c_str(), -1, SQLITE_TRANSIENT);

  rc = sqlite3_step(stmt);
  sqlite3_finalize(stmt);
  if (rc != SQLITE_DONE) {
    error_out = std::string("insert failed: ") + sqlite3_errmsg(db_);
    sqlite3_exec(db_, "ROLLBACK;", nullptr, nullptr, nullptr);
    return false;
  }

  rc = sqlite3_exec(db_, "COMMIT;", nullptr, nullptr, nullptr);
  if (rc != SQLITE_OK) {
    error_out = std::string("COMMIT failed: ") + sqlite3_errmsg(db_);
    sqlite3_exec(db_, "ROLLBACK;", nullptr, nullptr, nullptr);
    return false;
  }

  hash_ = hash;
  return true;
}

CommitResult ConfigStore::commit_locked(
  double expected_version, Snapshot candidate, bool partial)
{
  CommitResult r;
  r.version = version_;
  r.snapshot = current_;

  // 1) Optimistic concurrency check. Versions are integer-valued doubles
  //    (0, 1, 2, ...) and therefore exactly representable; a plain compare is
  //    an unambiguous compare-and-swap token.
  if (expected_version != version_) {
    r.code = CommitCode::REJECT_STALE_VERSION;
    std::ostringstream os;
    os << "stale version: current is " << fmt_num(version_)
       << " but request carried expected_version=" << fmt_num(expected_version);
    r.message = os.str();
    return r;
  }

  // 2) Merge partial fields, then validate the ENTIRE resulting snapshot.
  //    NaN never equals the KEEP sentinel, so an explicit NaN flows through to
  //    validate() and is rejected there (as a non-finite value).
  Snapshot merged = current_;
  if (partial) {
    if (candidate.sampling_rate_hz != KEEP) {
      merged.sampling_rate_hz = candidate.sampling_rate_hz;
    }
    if (candidate.cache_length_s != KEEP) {
      merged.cache_length_s = candidate.cache_length_s;
    }
    if (candidate.allowed_latency_s != KEEP) {
      merged.allowed_latency_s = candidate.allowed_latency_s;
    }
  } else {
    merged = candidate;
  }

  Validation v = validate(merged);
  if (!v) {
    r.code = CommitCode::REJECT_CONSTRAINT;
    r.message = v.reason;
    return r;
  }

  // 3) Persist atomically (new append-only row). Any failure rolls back and
  //    leaves the in-memory committed state completely untouched.
  const double new_version = version_ + 1.0;
  std::string db_error;
  if (!insert_row(new_version, merged, db_error)) {
    r.code = CommitCode::ERR_PERSISTENCE;
    r.message = "configuration not stored: " + db_error +
                "; in-memory state unchanged at version " +
                fmt_num(version_);
    return r;
  }

  // 4) Durable commit landed; advance in-memory state, then notify.
  const Snapshot old_snap = current_;
  current_ = merged;
  version_ = new_version;
  commit_seq_ += 1;
  r.code = CommitCode::OK;
  r.message = "committed version " + fmt_num(new_version);
  r.version = new_version;
  r.snapshot = merged;

  if (callback_) {
    // callback_active_for (thread_local in commit()) is already set for this
    // store, so any commit() the callback attempts is rejected before it can
    // take effect. Exceptions propagate after clearing state via the outer
    // commit() guard.
    callback_(old_snap, merged, new_version);
  }
  return r;
}

CommitResult ConfigStore::commit(
  double expected_version, const Snapshot & candidate, bool partial)
{
  // Re-entrancy guard checked BEFORE taking the mutex: a change callback runs
  // on this same thread while the mutex is held, so trying to lock again would
  // dead-lock. A commit attempted from within a callback is rejected instead.
  // thread_local tracks which store's callback is active on this thread.
  thread_local const ConfigStore * callback_active_for = nullptr;
  if (callback_active_for == this) {
    // Read state under the lock so the reported version is current.
    std::lock_guard<std::recursive_mutex> lock(mutex_);
    CommitResult r;
    r.code = CommitCode::REJECT_UPDATE_IN_CALLBACK;
    r.message =
      "cannot commit a new configuration from inside a change callback";
    r.version = version_;
    r.snapshot = current_;
    return r;
  }

  std::lock_guard<std::recursive_mutex> lock(mutex_);
  callback_active_for = this;
  struct Guard
  {
    const ConfigStore *& slot;
    ~Guard() { slot = nullptr; }  // clear even if the callback throws
  } guard{callback_active_for};
  return commit_locked(expected_version, candidate, partial);
}

}  // namespace robot_param_atomic
