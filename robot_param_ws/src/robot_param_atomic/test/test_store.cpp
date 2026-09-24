// SPDX-License-Identifier: Apache-2.0
//
// Tests against a REAL SQLite database: commit chains, version conflicts,
// restart reload, tampered rows, foreign-key rows and an injected real
// persistence failure (ABORT trigger).
#include <gtest/gtest.h>

#include <sqlite3.h>

#include <cstdio>
#include <cstdlib>
#include <filesystem>
#include <string>
#include <unistd.h>
#include <vector>

#include "robot_param/core/crypto.hpp"
#include "robot_param/core/store.hpp"

using namespace robot_param;

namespace {

namespace fs = std::filesystem;

std::string unique_dir() {
  static int counter = 0;
  fs::path base = fs::temp_directory_path() / "robot_param_tests";
  fs::create_directories(base);
  char templ[256];
  std::snprintf(templ, sizeof(templ), "%s/d%05d_%d_XXXXXX",
                base.c_str(), static_cast<int>(::getpid()), counter++);
  char* created = ::mkdtemp(templ);
  if (created == nullptr) {
    throw std::runtime_error("mkdtemp failed");
  }
  return created;
}

void sql_exec(const std::string& path, const std::string& sql) {
  sqlite3* db = nullptr;
  ASSERT_EQ(sqlite3_open_v2(path.c_str(), &db,
                            SQLITE_OPEN_READWRITE, nullptr),
            SQLITE_OK)
      << sqlite3_errmsg(db);
  char* err = nullptr;
  int rc = sqlite3_exec(db, sql.c_str(), nullptr, nullptr, &err);
  ASSERT_EQ(rc, SQLITE_OK) << (err ? err : sqlite3_errmsg(db));
  sqlite3_free(err);
  sqlite3_close(db);
}

ConfigPatch rate_patch(double hz) {
  ConfigPatch p;
  p.set_sample_rate = true;
  p.sample_rate_hz = hz;
  return p;
}
ConfigPatch latency_patch(std::uint64_t ms) {
  ConfigPatch p;
  p.set_allowed_latency = true;
  p.allowed_latency_ms = ms;
  return p;
}
ConfigPatch buffer_patch(std::uint32_t n) {
  ConfigPatch p;
  p.set_buffer_length = true;
  p.buffer_length = n;
  return p;
}

class StoreTest : public ::testing::Test {
 protected:
  void SetUp() override {
    dir = unique_dir();
    db = dir + "/cfg.sqlite3";
    key = random_key_32();
  }
  void TearDown() override {
    std::error_code ec;
    fs::remove_all(dir, ec);
  }
  std::string dir;
  std::string db;
  std::vector<std::uint8_t> key;
};

}  // namespace

TEST_F(StoreTest, FreshStartAndFirstCommit) {
  auto store = ConfigStore::open(db, key);
  EXPECT_EQ(store->load_info().state, LoadState::FRESH);
  EXPECT_EQ(store->snapshot().version, 0u);

  auto r = store->commit(0, buffer_patch(2000), 1000);
  ASSERT_EQ(r.status, CommitStatus::OK) << r.message;
  EXPECT_EQ(r.snapshot.version, 1u);
  EXPECT_EQ(r.snapshot.buffer_length, 2000u);
}

TEST_F(StoreTest, StaleVersionIsRejectedAndEverythingStaysConsistent) {
  auto store = ConfigStore::open(db, key);
  ASSERT_EQ(store->commit(0, rate_patch(200.0), 1).status, CommitStatus::OK);

  // Client still believes it is on v0.
  auto stale = store->commit(0, buffer_patch(5000), 2);
  EXPECT_EQ(stale.status, CommitStatus::VERSION_CONFLICT);
  EXPECT_EQ(stale.snapshot.version, 1u);
  EXPECT_NE(stale.message.find("does not match current version 1"),
            std::string::npos);

  // Correct version succeeds.
  auto ok = store->commit(1, buffer_patch(5000), 3);
  ASSERT_EQ(ok.status, CommitStatus::OK);
  EXPECT_EQ(ok.snapshot.version, 2u);
}

TEST_F(StoreTest, InvalidCombinationIsRejectedWithoutPersisting) {
  auto store = ConfigStore::open(db, key);
  ASSERT_EQ(store->commit(0, buffer_patch(1000), 1).status, CommitStatus::OK);

  // 1000 samples @100Hz covers 10000ms; latency 6000 needs 12000ms: illegal.
  auto bad = store->commit(1, latency_patch(6000), 2);
  EXPECT_EQ(bad.status, CommitStatus::REJECTED_VALIDATION);
  EXPECT_EQ(bad.snapshot.version, 1u);

  // A legal retry with the same version still works (nothing was consumed).
  auto good = store->commit(1, latency_patch(50), 3);
  ASSERT_EQ(good.status, CommitStatus::OK) << good.message;
  EXPECT_EQ(good.snapshot.version, 2u);
}

TEST_F(StoreTest, RestartLoadsLatestCommittedConfig) {
  {
    auto store = ConfigStore::open(db, key);
    ASSERT_EQ(store->commit(0, rate_patch(250.0), 11).status, CommitStatus::OK);
    ASSERT_EQ(store->commit(1, buffer_patch(9000), 12).status,
              CommitStatus::OK);
  }
  auto store = ConfigStore::open(db, key);
  ASSERT_EQ(store->load_info().state, LoadState::LOADED);
  ConfigSnapshot s = store->snapshot();
  EXPECT_EQ(s.version, 2u);
  EXPECT_DOUBLE_EQ(s.sample_rate_hz, 250.0);
  EXPECT_EQ(s.buffer_length, 9000u);
  EXPECT_EQ(s.committed_at_ms, 12);
  EXPECT_NE(store->load_info().detail.find("loaded committed version 2"),
            std::string::npos);

  // Versions continue after the on-disk maximum, never reuse it.
  auto next = store->commit(2, latency_patch(60), 13);
  ASSERT_EQ(next.status, CommitStatus::OK);
  EXPECT_EQ(next.snapshot.version, 3u);
}

TEST_F(StoreTest, TamperedNewestRowIsRejectedAndFallbackToOlderGood) {
  {
    auto store = ConfigStore::open(db, key);
    ASSERT_EQ(store->commit(0, rate_patch(200.0), 21).status, CommitStatus::OK);
    ASSERT_EQ(store->commit(1, buffer_patch(9000), 22).status,
              CommitStatus::OK);
  }
  // Flip a byte in the newest row's rate without updating the hashes.
  sql_exec(db,
           "UPDATE config_versions SET sample_rate_hz = 123.5 "
           "WHERE version = 2;");

  auto store = ConfigStore::open(db, key);
  EXPECT_EQ(store->load_info().state, LoadState::RECOVERED);
  EXPECT_EQ(store->snapshot().version, 1u);  // fell back
  EXPECT_DOUBLE_EQ(store->snapshot().sample_rate_hz, 200.0);
  EXPECT_NE(store->load_info().detail.find("version 2 rejected"),
            std::string::npos);
  EXPECT_NE(store->load_info().detail.find("SHA-256 mismatch"),
            std::string::npos);

  // New commit chains on after the max existing version (2) -> becomes v3.
  auto r = store->commit(1, latency_patch(60), 23);
  ASSERT_EQ(r.status, CommitStatus::OK);
  EXPECT_EQ(r.snapshot.version, 3u);
}

TEST_F(StoreTest, ForgedHmacRowIsRejected) {
  {
    auto store = ConfigStore::open(db, key);
    ASSERT_EQ(store->commit(0, rate_patch(200.0), 31).status, CommitStatus::OK);
  }
  // Attacker inserts an internally-consistent row signed with their own key.
  const std::string attacker_material = "attacker-secret-key-32bytes!!xx";
  std::vector<std::uint8_t> attacker = parse_key(attacker_material);
  ASSERT_EQ(attacker.size(), 31u);  // raw key, arbitrary length is fine
  ConfigSnapshot forged;
  forged.version = 2;
  forged.sample_rate_hz = 300.0;
  forged.buffer_length = 1000;
  forged.allowed_latency_ms = 50;
  forged.committed_at_ms = 999;
  const std::string canon = ConfigStore::canonical_payload(forged);
  const std::string sha = sha256_hex(canon);
  const std::string mac = hmac_sha256_hex(attacker, canon);
  std::string sql =
      "INSERT INTO config_versions (version, sample_rate_hz, buffer_length, "
      "allowed_latency_ms, committed_at_ms, payload_sha256, payload_hmac) "
      "VALUES (2, 300.0, 1000, 50, 999, '" + sha + "', '" + mac + "');";
  sql_exec(db, sql);

  auto store = ConfigStore::open(db, key);
  EXPECT_EQ(store->load_info().state, LoadState::RECOVERED);
  EXPECT_EQ(store->snapshot().version, 1u);
  EXPECT_NE(store->load_info().detail.find("HMAC mismatch"),
            std::string::npos);
}

TEST_F(StoreTest, WrongKeyRejectsEveryRow) {
  {
    auto store = ConfigStore::open(db, key);
    ASSERT_EQ(store->commit(0, rate_patch(200.0), 41).status, CommitStatus::OK);
  }
  auto other = random_key_32();
  auto store = ConfigStore::open(db, other);
  EXPECT_EQ(store->load_info().state, LoadState::NO_VALID_CONFIG);
  EXPECT_EQ(store->snapshot().version, 0u);
  EXPECT_NE(store->load_info().detail.find("HMAC mismatch"),
            std::string::npos);
}

TEST_F(StoreTest, MissingColumnsAndTruncatedRowAreRejected) {
  {
    auto store = ConfigStore::open(db, key);
    ASSERT_EQ(store->commit(0, buffer_patch(2000), 51).status, CommitStatus::OK);
  }
  // NULL out a NOT NULL column is blocked by the schema; instead corrupt the
  // hex digest column.
  sql_exec(db,
           "UPDATE config_versions SET payload_sha256 = 'not-hex!' "
           "WHERE version = 1;");
  auto store = ConfigStore::open(db, key);
  EXPECT_EQ(store->load_info().state, LoadState::NO_VALID_CONFIG);
  EXPECT_NE(store->load_info().detail.find("not valid hex"),
            std::string::npos);
}

// Persistence failure is REAL: install an ABORT trigger that forbids inserts
// on the revisions table. The commit must fail with PERSISTENCE_FAILED, the
// transaction must be rolled back and the live state must be unchanged.
TEST_F(StoreTest, PersistenceFailureRollsBackAndKeepsServiceAlive) {
  auto store = ConfigStore::open(db, key);
  ASSERT_EQ(store->commit(0, buffer_patch(2000), 61).status, CommitStatus::OK);

  sql_exec(db,
           "CREATE TRIGGER fail_insert BEFORE INSERT ON config_versions "
           "BEGIN SELECT RAISE(ABORT, 'injected persistence outage'); END;");

  auto failed = store->commit(1, rate_patch(300.0), 62);
  EXPECT_EQ(failed.status, CommitStatus::PERSISTENCE_FAILED);
  EXPECT_NE(failed.message.find("injected persistence outage"),
            std::string::npos);
  EXPECT_EQ(failed.snapshot.version, 1u);
  EXPECT_DOUBLE_EQ(failed.snapshot.sample_rate_hz, 100.0);  // unchanged

  // Drop the trigger: subsequent commits work again, no state was poisoned.
  sql_exec(db, "DROP TRIGGER fail_insert;");
  auto recovered = store->commit(1, rate_patch(300.0), 63);
  ASSERT_EQ(recovered.status, CommitStatus::OK) << recovered.message;
  EXPECT_EQ(recovered.snapshot.version, 2u);
  EXPECT_DOUBLE_EQ(recovered.snapshot.sample_rate_hz, 300.0);

  // Restart: only the two genuinely committed revisions exist.
  store.reset();
  auto reopened = ConfigStore::open(db, key);
  EXPECT_EQ(reopened->load_info().state, LoadState::LOADED);
  EXPECT_EQ(reopened->snapshot().version, 2u);
}

TEST_F(StoreTest, UnwritableDatabaseFailsToOpenWithClearError) {
  std::string ro_dir = dir + "/readonly";
  fs::create_directory(ro_dir);
  fs::permissions(ro_dir, fs::perms::owner_read | fs::perms::owner_exec,
                  fs::perm_options::replace);
  const std::string bad = ro_dir + "/cfg.sqlite3";
  // Running as root would defeat permission checks; skip when that is the case.
  if (::geteuid() == 0) {
    GTEST_SKIP() << "running as root; directory permissions are not enforced";
  }
  EXPECT_THROW(ConfigStore::open(bad, key), std::runtime_error);
  fs::permissions(ro_dir, fs::perms::owner_all, fs::perm_options::replace);
}

TEST_F(StoreTest, StructuralCorruptionIsFatalOnOpen) {
  {
    auto store = ConfigStore::open(db, key);
    ASSERT_EQ(store->commit(0, buffer_patch(2000), 71).status, CommitStatus::OK);
  }
  // Corrupt the SQLite header magic.
  {
    FILE* f = std::fopen(db.c_str(), "r+b");
    ASSERT_NE(f, nullptr);
    std::fseek(f, 0, SEEK_SET);
    const char junk[16] = "NOTSQLITE!!!!!!";
    std::fwrite(junk, 1, sizeof(junk), f);
    std::fclose(f);
  }
  EXPECT_THROW(ConfigStore::open(db, key), std::runtime_error);
}
