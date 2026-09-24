// SPDX-License-Identifier: Apache-2.0
#include "robot_param/core/store.hpp"

#include <sqlite3.h>

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <filesystem>
#include <iomanip>
#include <sstream>
#include <stdexcept>

namespace robot_param {

namespace {

constexpr const char* kSchemaSql = R"SQL(
CREATE TABLE IF NOT EXISTS config_versions (
  version            INTEGER PRIMARY KEY,
  sample_rate_hz     REAL    NOT NULL,
  buffer_length      INTEGER NOT NULL,
  allowed_latency_ms INTEGER NOT NULL,
  committed_at_ms    INTEGER NOT NULL,
  payload_sha256     TEXT    NOT NULL,
  payload_hmac       TEXT    NOT NULL
);
)SQL";

void exec_or_throw(sqlite3* db, const char* sql) {
  char* err = nullptr;
  if (sqlite3_exec(db, sql, nullptr, nullptr, &err) != SQLITE_OK) {
    std::string msg = err ? err : "unknown SQLite error";
    sqlite3_free(err);
    throw std::runtime_error("SQLite exec failed: " + msg);
  }
}

std::string canonical_of(std::uint64_t version, double rate,
                         std::uint32_t buffer, std::uint64_t latency,
                         std::int64_t committed_at_ms) {
  std::ostringstream oss;
  oss << "version=" << version << '\n'
      << "sample_rate_hz=" << std::setprecision(17) << rate << '\n'
      << "buffer_length=" << buffer << '\n'
      << "allowed_latency_ms=" << latency << '\n'
      << "committed_at_ms=" << committed_at_ms << '\n';
  return oss.str();
}

}  // namespace

struct ConfigStore::Stmt {
  sqlite3_stmt* stmt = nullptr;
  Stmt(sqlite3* db, const char* sql) {
    if (sqlite3_prepare_v2(db, sql, -1, &stmt, nullptr) != SQLITE_OK) {
      throw std::runtime_error(std::string("prepare failed: ") +
                               sqlite3_errmsg(db) + " for SQL: " + sql);
    }
  }
  ~Stmt() {
    if (stmt) {
      sqlite3_finalize(stmt);
    }
  }
  Stmt(const Stmt&) = delete;
  Stmt& operator=(const Stmt&) = delete;
};

std::unique_ptr<ConfigStore> ConfigStore::open(
    const std::string& db_path, std::vector<std::uint8_t> key) {
  if (key.empty()) {
    throw std::runtime_error("HMAC key must not be empty");
  }
  std::filesystem::path p(db_path);
  if (p.has_parent_path()) {
    std::error_code ec;
    std::filesystem::create_directories(p.parent_path(), ec);
    if (ec) {
      throw std::runtime_error("cannot create database directory " +
                               p.parent_path().string() + ": " + ec.message());
    }
  }

  std::unique_ptr<ConfigStore> store(new ConfigStore());
  store->db_path_ = db_path;
  store->key_ = std::move(key);

  if (sqlite3_open_v2(db_path.c_str(), &store->db_,
                      SQLITE_OPEN_READWRITE | SQLITE_OPEN_CREATE |
                          SQLITE_OPEN_FULLMUTEX,
                      nullptr) != SQLITE_OK) {
    std::string msg;
    if (store->db_) {
      msg = sqlite3_errmsg(store->db_);
    } else {
      msg = "out of memory";
    }
    // sqlite3_open_v2 may allocate db even on failure.
    if (store->db_) {
      sqlite3_close(store->db_);
      store->db_ = nullptr;
    }
    throw std::runtime_error("cannot open database " + db_path + ": " + msg);
  }
  sqlite3_busy_timeout(store->db_, 5000);

  try {
    store->initialize();
  } catch (...) {
    if (store->db_) {
      sqlite3_close(store->db_);
      store->db_ = nullptr;
    }
    throw;
  }
  return store;
}

ConfigStore::~ConfigStore() {
  if (db_) {
    sqlite3_close(db_);
  }
}

void ConfigStore::initialize() {
  exec_or_throw(db_, "PRAGMA journal_mode=DELETE;");
  exec_or_throw(db_, "PRAGMA synchronous=FULL;");
  exec_or_throw(db_, "PRAGMA foreign_keys=ON;");
  exec_or_throw(db_, kSchemaSql);

  // Structural integrity: any output other than the single row "ok" is fatal.
  {
    Stmt q(db_, "PRAGMA quick_check;");
    std::string report;
    int rows = 0;
    int rc;
    while ((rc = sqlite3_step(q.stmt)) == SQLITE_ROW) {
      ++rows;
      const unsigned char* text = sqlite3_column_text(q.stmt, 0);
      if (text) {
        if (!report.empty()) {
          report += "; ";
        }
        report += reinterpret_cast<const char*>(text);
      }
    }
    if (rc != SQLITE_DONE) {
      throw std::runtime_error(std::string("PRAGMA quick_check failed: ") +
                               sqlite3_errmsg(db_));
    }
    if (rows != 1 || report != "ok") {
      throw std::runtime_error(
          "database structural corruption (PRAGMA quick_check): " + report);
    }
  }

  reload_from_disk();
}

void ConfigStore::reload_from_disk() {
  Stmt q(db_,
         "SELECT version, sample_rate_hz, buffer_length, allowed_latency_ms, "
         "committed_at_ms, payload_sha256, payload_hmac FROM config_versions "
         "ORDER BY version DESC;");

  std::uint64_t max_version = 0;
  bool any_row = false;
  bool newest_is_good = true;  // meaning: the first row scanned is good
  ConfigSnapshot newest_good;
  bool have_good = false;
  std::vector<std::string> corruptions;

  int rc;
  bool first = true;
  while ((rc = sqlite3_step(q.stmt)) == SQLITE_ROW) {
    any_row = true;

    std::uint64_t version = 0;
    bool row_bad = false;
    std::string reason;

    auto fail = [&](const std::string& why) {
      if (!row_bad) {
        row_bad = true;
        reason = why;
      }
    };

    if (sqlite3_column_type(q.stmt, 0) != SQLITE_INTEGER) {
      fail("version column missing or wrong type");
    } else {
      sqlite3_int64 v = sqlite3_column_int64(q.stmt, 0);
      if (v < 1) {
        fail("version must be >= 1");
      } else {
        version = static_cast<std::uint64_t>(v);
      }
    }

    double rate = 0.0;
    if (sqlite3_column_type(q.stmt, 1) != SQLITE_FLOAT) {
      fail("sample_rate_hz column missing or wrong type");
    } else {
      rate = sqlite3_column_double(q.stmt, 1);
      if (!std::isfinite(rate)) {
        fail("sample_rate_hz is not finite");
      }
    }

    std::uint32_t buffer = 0;
    if (sqlite3_column_type(q.stmt, 2) != SQLITE_INTEGER) {
      fail("buffer_length column missing or wrong type");
    } else {
      sqlite3_int64 v = sqlite3_column_int64(q.stmt, 2);
      if (v < 0 || v > 0xffffffffll) {
        fail("buffer_length out of uint32 range");
      } else {
        buffer = static_cast<std::uint32_t>(v);
      }
    }

    std::uint64_t latency = 0;
    if (sqlite3_column_type(q.stmt, 3) != SQLITE_INTEGER) {
      fail("allowed_latency_ms column missing or wrong type");
    } else {
      sqlite3_int64 v = sqlite3_column_int64(q.stmt, 3);
      if (v < 0) {
        fail("allowed_latency_ms is negative");
      } else {
        latency = static_cast<std::uint64_t>(v);
      }
    }

    std::int64_t committed = 0;
    if (sqlite3_column_type(q.stmt, 4) != SQLITE_INTEGER) {
      fail("committed_at_ms column missing or wrong type");
    } else {
      committed = sqlite3_column_int64(q.stmt, 4);
    }

    const unsigned char* sha_text = sqlite3_column_text(q.stmt, 5);
    const unsigned char* hmac_text = sqlite3_column_text(q.stmt, 6);
    if (sha_text == nullptr) {
      fail("payload_sha256 missing");
    }
    if (hmac_text == nullptr) {
      fail("payload_hmac missing");
    }

    if (!row_bad) {
      max_version = std::max(max_version, version);

      const std::string payload =
          canonical_of(version, rate, buffer, latency, committed);
      const std::string stored_sha =
          reinterpret_cast<const char*>(sha_text);
      const std::string stored_hmac =
          reinterpret_cast<const char*>(hmac_text);

      const auto actual_sha = sha256(payload);
      const auto expected_sha = from_hex(stored_sha);
      if (expected_sha.empty()) {
        fail("payload_sha256 is not valid hex");
      } else if (!constant_time_equal(actual_sha, expected_sha)) {
        fail("SHA-256 mismatch: payload tampered with or bit-rotted");
      }

      if (!row_bad) {
        const auto actual_hmac = hmac_sha256(key_, payload);
        const auto expected_hmac = from_hex(stored_hmac);
        if (expected_hmac.empty()) {
          fail("payload_hmac is not valid hex");
        } else if (!constant_time_equal(actual_hmac, expected_hmac)) {
          fail("HMAC mismatch: row was not written with this node key");
        }
      }

      if (!row_bad) {
        ConfigSnapshot s;
        s.version = version;
        s.sample_rate_hz = rate;
        s.buffer_length = buffer;
        s.allowed_latency_ms = latency;
        s.committed_at_ms = committed;
        if (auto c = check_snapshot(s); !c.ok) {
          fail(std::string("stored values violate constraints: ") + c.error);
        } else if (!have_good) {
          newest_good = s;
          have_good = true;
        }
      }
    }

    if (first) {
      newest_is_good = !row_bad;
      first = false;
    }
    if (row_bad) {
      std::ostringstream oss;
      oss << "version " << (version ? std::to_string(version) : std::string("?"))
          << " rejected (" << reason << ")";
      corruptions.push_back(oss.str());
    }
  }

  if (rc != SQLITE_DONE) {
    throw std::runtime_error(std::string("reading config rows failed: ") +
                             sqlite3_errmsg(db_));
  }

  std::lock_guard<std::mutex> lock(mutex_);
  next_version_ = max_version + 1;
  load_info_ = LoadInfo{};

  if (!any_row) {
    load_info_.state = LoadState::FRESH;
    load_info_.detail = "no committed configuration found; using built-in defaults";
    return;
  }
  if (newest_is_good && have_good) {
    current_ = newest_good;
    load_info_.state = LoadState::LOADED;
    std::ostringstream oss;
    oss << "loaded committed version " << current_.version;
    load_info_.detail = oss.str();
    return;
  }
  if (have_good) {
    current_ = newest_good;
    load_info_.state = LoadState::RECOVERED;
    std::ostringstream oss;
    oss << "newest committed revision(s) corrupt, fell back to version "
        << current_.version << "; rejected rows: "
        << [&] {
             std::string out;
             for (std::size_t i = 0; i < corruptions.size(); ++i) {
               if (i) {
                 out += "; ";
               }
               out += corruptions[i];
             }
             return out;
           }();
    load_info_.detail = oss.str();
    return;
  }

  load_info_.state = LoadState::NO_VALID_CONFIG;
  load_info_.detail =
      "all committed revisions are corrupt, using built-in defaults; rejected: " +
      [&] {
        std::string out;
        for (std::size_t i = 0; i < corruptions.size(); ++i) {
          if (i) {
            out += "; ";
          }
          out += corruptions[i];
        }
        return out;
      }();
}

std::string ConfigStore::canonical_payload(const ConfigSnapshot& s) {
  return canonical_of(s.version, s.sample_rate_hz, s.buffer_length,
                      s.allowed_latency_ms, s.committed_at_ms);
}

CommitResult ConfigStore::commit(std::uint64_t expected_version,
                                 const ConfigPatch& patch,
                                 std::int64_t now_ms) {
  std::lock_guard<std::mutex> lock(mutex_);

  if (expected_version != current_.version) {
    CommitResult r;
    r.status = CommitStatus::VERSION_CONFLICT;
    r.snapshot = current_;
    std::ostringstream oss;
    oss << "expected_version " << expected_version
        << " does not match current version " << current_.version
        << "; refresh and retry";
    r.message = oss.str();
    return r;
  }

  ConfigSnapshot candidate = apply_patch(current_, patch);
  candidate.version = next_version_;
  candidate.committed_at_ms = now_ms;

  if (auto c = check_snapshot(candidate); !c.ok) {
    CommitResult r;
    r.status = CommitStatus::REJECTED_VALIDATION;
    r.snapshot = current_;
    r.message = c.error;
    return r;
  }

  const std::string payload = canonical_payload(candidate);
  const std::string sha_hex = sha256_hex(payload);
  const std::string hmac_hex = hmac_sha256_hex(key_, payload);

  auto abort_txn = [&] {
    char* err = nullptr;
    sqlite3_exec(db_, "ROLLBACK;", nullptr, nullptr, &err);
    if (err) {
      sqlite3_free(err);
    }
  };

  char* begin_err = nullptr;
  if (sqlite3_exec(db_, "BEGIN IMMEDIATE;", nullptr, nullptr, &begin_err) !=
      SQLITE_OK) {
    std::string msg = begin_err ? begin_err : sqlite3_errmsg(db_);
    sqlite3_free(begin_err);
    CommitResult r;
    r.status = CommitStatus::PERSISTENCE_FAILED;
    r.snapshot = current_;
    r.message = std::string("BEGIN IMMEDIATE failed: ") + msg;
    return r;
  }

  try {
    Stmt ins(
        db_,
        "INSERT INTO config_versions (version, sample_rate_hz, buffer_length, "
        "allowed_latency_ms, committed_at_ms, payload_sha256, payload_hmac) "
        "VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7);");
    sqlite3_bind_int64(ins.stmt, 1,
                       static_cast<sqlite3_int64>(candidate.version));
    sqlite3_bind_double(ins.stmt, 2, candidate.sample_rate_hz);
    sqlite3_bind_int64(ins.stmt, 3, candidate.buffer_length);
    sqlite3_bind_int64(ins.stmt, 4,
                       static_cast<sqlite3_int64>(candidate.allowed_latency_ms));
    sqlite3_bind_int64(ins.stmt, 5, candidate.committed_at_ms);
    sqlite3_bind_text(ins.stmt, 6, sha_hex.c_str(),
                      static_cast<int>(sha_hex.size()), SQLITE_TRANSIENT);
    sqlite3_bind_text(ins.stmt, 7, hmac_hex.c_str(),
                      static_cast<int>(hmac_hex.size()), SQLITE_TRANSIENT);

    int rc = sqlite3_step(ins.stmt);
    if (rc != SQLITE_DONE) {
      std::string dbmsg = sqlite3_errmsg(db_);
      abort_txn();
      CommitResult r;
      r.status = CommitStatus::PERSISTENCE_FAILED;
      r.snapshot = current_;
      r.message = std::string("INSERT failed: ") + dbmsg;
      return r;
    }
  } catch (const std::exception& e) {
    abort_txn();
    CommitResult r;
    r.status = CommitStatus::PERSISTENCE_FAILED;
    r.snapshot = current_;
    r.message = std::string("statement preparation failed: ") + e.what();
    return r;
  }

  char* commit_err = nullptr;
  if (sqlite3_exec(db_, "COMMIT;", nullptr, nullptr, &commit_err) != SQLITE_OK) {
    std::string msg = commit_err ? commit_err : sqlite3_errmsg(db_);
    sqlite3_free(commit_err);
    abort_txn();
    CommitResult r;
    r.status = CommitStatus::PERSISTENCE_FAILED;
    r.snapshot = current_;
    r.message = std::string("COMMIT failed: ") + msg;
    return r;
  }

  current_ = candidate;
  ++next_version_;

  CommitResult r;
  r.status = CommitStatus::OK;
  r.snapshot = current_;
  r.message = "committed";
  return r;
}

ConfigSnapshot ConfigStore::snapshot() const {
  std::lock_guard<std::mutex> lock(mutex_);
  return current_;
}

LoadInfo ConfigStore::load_info() const {
  std::lock_guard<std::mutex> lock(mutex_);
  return load_info_;
}

}  // namespace robot_param
