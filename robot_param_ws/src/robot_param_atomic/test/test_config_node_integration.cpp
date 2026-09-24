// SPDX-License-Identifier: Apache-2.0
//
// In-process ROS 2 integration tests: real rclcpp services on a real
// MultiThreadedExecutor, real parameter clients, real SQLite.
#include <gtest/gtest.h>

#include <sqlite3.h>

#include <atomic>
#include <chrono>
#include <filesystem>
#include <future>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <unistd.h>
#include <vector>

#include <rclcpp/rclcpp.hpp>

#include "robot_param/core/crypto.hpp"
#include "robot_param/node/robot_config_node.hpp"
#include "robot_param_atomic/msg/config_snapshot.hpp"
#include "robot_param_atomic/srv/atomic_update.hpp"
#include "robot_param_atomic/srv/get_config.hpp"

using namespace std::chrono_literals;
using robot_param::CommitStatus;
using robot_param::ConfigPatch;
using robot_param::ConfigSnapshot;
using robot_param::ConfigStore;
using robot_param::LoadState;
using robot_param::RobotConfigNode;
using robot_param_atomic::srv::AtomicUpdate;
using robot_param_atomic::srv::GetConfig;
using ConfigSnapshotMsg = robot_param_atomic::msg::ConfigSnapshot;

namespace {

namespace fs = std::filesystem;

std::string unique_dir() {
  static std::atomic<int> counter{0};
  fs::path base = fs::temp_directory_path() / "robot_param_ros_tests";
  fs::create_directories(base);
  char templ[300];
  std::snprintf(templ, sizeof(templ), "%s/d%05d_%d_XXXXXX", base.c_str(),
                static_cast<int>(::getpid()), counter.fetch_add(1));
  return ::mkdtemp(templ);
}

std::string unique_node(const char* tag) {
  static std::atomic<int> n{0};
  return std::string("rc_") + tag + "_" +
         std::to_string(::getpid()) + "_" +
         std::to_string(n.fetch_add(1));
}

void sql_exec(const std::string& path, const std::string& sql) {
  sqlite3* db = nullptr;
  ASSERT_EQ(sqlite3_open_v2(path.c_str(), &db, SQLITE_OPEN_READWRITE, nullptr),
            SQLITE_OK);
  char* err = nullptr;
  ASSERT_EQ(sqlite3_exec(db, sql.c_str(), nullptr, nullptr, &err), SQLITE_OK)
      << (err ? err : "");
  sqlite3_free(err);
  sqlite3_close(db);
}

AtomicUpdate::Request::SharedPtr make_req(std::uint64_t expected, bool set_rate,
                                          double rate, bool set_buffer,
                                          std::uint32_t buffer,
                                          bool set_latency,
                                          std::uint64_t latency) {
  auto r = std::make_shared<AtomicUpdate::Request>();
  r->expected_version = expected;
  r->set_sample_rate = set_rate;
  r->sample_rate_hz = rate;
  r->set_buffer_length = set_buffer;
  r->buffer_length = buffer;
  r->set_allowed_latency = set_latency;
  r->allowed_latency_ms = latency;
  return r;
}

// One running server node + its own MultiThreadedExecutor thread.
struct Server {
  std::string name;
  std::string dir;
  std::string db;
  rclcpp::NodeOptions opts;
  std::shared_ptr<RobotConfigNode> node;
  rclcpp::executors::MultiThreadedExecutor::SharedPtr exec;
  std::thread spin_thread;

  explicit Server() {
    name = unique_node("srv");
    dir = unique_dir();
    db = dir + "/cfg.sqlite3";
    opts.arguments({"--ros-args", "-r", "__node:=" + name,
                    "-p", "db_path:=" + db});
    node = std::make_shared<RobotConfigNode>(opts);

    exec = std::make_shared<rclcpp::executors::MultiThreadedExecutor>(
        rclcpp::ExecutorOptions(), 4);
    exec->add_node(node);
    spin_thread = std::thread([this] { exec->spin(); });
  }

  ~Server() {
    exec->cancel();
    if (spin_thread.joinable()) {
      spin_thread.join();
    }
  }

  std::string service_base() const { return std::string("/") + name; }
};

// Client node with its own spinner thread, so service calls can be made from
// inside server-side callbacks without deadlocking.
struct Client {
  std::shared_ptr<rclcpp::Node> node;
  std::shared_ptr<rclcpp::executors::SingleThreadedExecutor> exec;
  std::thread spin_thread;
  rclcpp::Client<AtomicUpdate>::SharedPtr updater;
  rclcpp::Client<GetConfig>::SharedPtr getter;

  explicit Client(const std::string& base) {
    node = std::make_shared<rclcpp::Node>(unique_node("cli"));
    exec = std::make_shared<rclcpp::executors::SingleThreadedExecutor>();
    exec->add_node(node);
    spin_thread = std::thread([this] { exec->spin(); });

    updater = node->create_client<AtomicUpdate>(base + "/update");
    getter = node->create_client<GetConfig>(base + "/get");
    if (!updater->wait_for_service(15s) || !getter->wait_for_service(15s)) {
      construction_failed_ = true;
    }
  }

  bool construction_failed_ = false;

  ~Client() {
    exec->cancel();
    if (spin_thread.joinable()) {
      spin_thread.join();
    }
  }

  std::shared_ptr<AtomicUpdate::Response> update(
      const AtomicUpdate::Request::SharedPtr& req) {
    auto fut = updater->async_send_request(req);
    EXPECT_EQ(fut.wait_for(15s), std::future_status::ready)
        << "update call timed out";
    if (fut.wait_for(0s) != std::future_status::ready) {
      return nullptr;
    }
    return fut.get();
  }

  std::shared_ptr<GetConfig::Response> get() {
    auto fut = getter->async_send_request(std::make_shared<GetConfig::Request>());
    EXPECT_EQ(fut.wait_for(15s), std::future_status::ready)
        << "get call timed out";
    return fut.get();
  }
};

}  // namespace

class NodeIntegration : public ::testing::Test {
 protected:
  static void SetUpTestSuite() {
    if (!rclcpp::ok()) {
      rclcpp::init(0, nullptr);
    }
  }
};

TEST_F(NodeIntegration, GetFreshDefaults) {
  Server srv;
  Client cli(srv.service_base());
  ASSERT_FALSE(cli.construction_failed_);
  auto g = cli.get();
  ASSERT_NE(g, nullptr);
  EXPECT_EQ(g->version, 0u);
  EXPECT_DOUBLE_EQ(g->sample_rate_hz, 100.0);
  EXPECT_EQ(g->buffer_length, 1000u);
  EXPECT_EQ(g->allowed_latency_ms, 50u);
  EXPECT_EQ(g->load_state, static_cast<std::uint8_t>(LoadState::FRESH));
}

TEST_F(NodeIntegration, SingleFieldLegalButCombinationIllegalViaService) {
  Server srv;
  Client cli(srv.service_base());
  ASSERT_FALSE(cli.construction_failed_);

  // Each individual value is in range: rate=1000 Hz (ok), buffer=100 samples
  // (ok), latency=100 ms (ok). Combined: buffer covers 100/1000*1000 = 100 ms
  // but must cover 2*100 = 200 ms -> batch rejected wholesale.
  auto r = cli.update(make_req(0, true, 1000.0, true, 100, true, 100));
  ASSERT_NE(r, nullptr);
  ASSERT_EQ(r->status, static_cast<std::uint8_t>(CommitStatus::REJECTED_VALIDATION))
      << r->message;
  EXPECT_NE(r->message.find("cross-field constraint"), std::string::npos);
  // Service stayed at v0.
  EXPECT_EQ(r->version, 0u);

  auto g = cli.get();
  EXPECT_EQ(g->version, 0u);
  EXPECT_EQ(g->sample_rate_hz, 100.0);  // nothing applied
  EXPECT_EQ(g->buffer_length, 1000u);
  EXPECT_EQ(g->allowed_latency_ms, 50u);

  // The same field values in a legal combination (buffer 300 -> 300 ms)
  // commit fine.
  auto ok = cli.update(make_req(0, true, 1000.0, true, 300, true, 100));
  ASSERT_NE(ok, nullptr);
  ASSERT_EQ(ok->status, 0u) << ok->message;
  EXPECT_EQ(ok->version, 1u);
}

TEST_F(NodeIntegration, StaleVersionCommitIsRejected) {
  Server srv;
  Client cli(srv.service_base());
  ASSERT_FALSE(cli.construction_failed_);
  ASSERT_EQ(cli.update(make_req(0, false, 0, true, 2000, false, 0))->status, 0u);

  auto stale = cli.update(make_req(0, false, 0, false, 0, true, 60));
  ASSERT_NE(stale, nullptr);
  EXPECT_EQ(stale->status,
            static_cast<std::uint8_t>(CommitStatus::VERSION_CONFLICT));
  EXPECT_EQ(stale->version, 1u);
  EXPECT_NE(stale->message.find("expected_version 0"), std::string::npos);
}

TEST_F(NodeIntegration, CallbackSeesCompleteSnapshotAndReentrantUpdateRejected) {
  Server srv;

  std::promise<std::string> reentrant_status;
  std::promise<ConfigSnapshot> snapshot_seen;
  std::promise<void> nested_done;
  auto sf_reentrant = reentrant_status.get_future();
  auto sf_snapshot = snapshot_seen.get_future();
  auto sf_nested = nested_done.get_future();

  std::mutex m;
  std::vector<ConfigSnapshot> observed;
  std::size_t fires = 0;

  srv.node->add_change_listener(
      [&](const ConfigSnapshot& snap) {
        if (snap.version == 0) {
          return;  // initial replay
        }
        {
          std::lock_guard<std::mutex> lk(m);
          observed.push_back(snap);
          ++fires;
        }
        if (snap.version == 1) {
          snapshot_seen.set_value(snap);

          // Try to commit FROM INSIDE the callback. The callback only has a
          // const snapshot; it uses a real parameter client to call back into
          // the same node. This must be rejected REENTRANT (status 4), never
          // deadlock, and never let the callback mutate the revision.
          auto node2 = std::make_shared<rclcpp::Node>(unique_node("nested"));
          auto exec2 = std::make_shared<rclcpp::executors::SingleThreadedExecutor>();
          exec2->add_node(node2);
          std::thread t([exec2] { exec2->spin(); });

          auto c = node2->create_client<AtomicUpdate>(
              srv.service_base() + "/update");
          EXPECT_TRUE(c->wait_for_service(5s));
          auto req = make_req(1, false, 0, false, 0, true, 40);
          // Same-process caller (a change listener): marked inproc so the node
          // can enforce the read-only-callback rule.
          req->origin = "inproc:integration-listener";
          auto fut = c->async_send_request(req);
          ASSERT_EQ(fut.wait_for(10s), std::future_status::ready);
          auto resp = fut.get();
          reentrant_status.set_value(
              std::to_string(resp->status) + ":" + resp->message);

          exec2->cancel();
          t.join();
          nested_done.set_value();
        }
      });

  Client cli(srv.service_base());
  ASSERT_FALSE(cli.construction_failed_);
  auto ok = cli.update(make_req(0, false, 0, true, 2000, false, 0));
  ASSERT_NE(ok, nullptr);
  ASSERT_EQ(ok->status, 0u) << ok->message;
  EXPECT_EQ(ok->version, 1u);

  ASSERT_EQ(sf_nested.wait_for(10s), std::future_status::ready);
  const std::string re = sf_reentrant.get();
  EXPECT_EQ(re.substr(0, 1), "4") << re;
  EXPECT_NE(re.find("change callback"), std::string::npos);

  ConfigSnapshot seen = sf_snapshot.get();
  EXPECT_EQ(seen.version, 1u);
  EXPECT_EQ(seen.buffer_length, 2000u);
  EXPECT_DOUBLE_EQ(seen.sample_rate_hz, 100.0);       // complete snapshot
  EXPECT_EQ(seen.allowed_latency_ms, 50u);           // untouched field present

  // After the callback chain finished, a normal outside update works and
  // chains on v1 -> v2.
  auto later = cli.update(make_req(1, false, 0, false, 0, true, 40));
  ASSERT_NE(later, nullptr);
  EXPECT_EQ(later->status, 0u) << later->message;
  EXPECT_EQ(later->version, 2u);
  EXPECT_EQ(later->allowed_latency_ms, 40u);
}

TEST_F(NodeIntegration, EightConcurrentClientsExactlyOneWinsRestRetry) {
  Server srv;

  auto attempt = [&](std::uint64_t expected, std::uint64_t latency) {
    Client c(srv.service_base());
    if (c.construction_failed_) ADD_FAILURE() << "client construction failed";
    return c.update(make_req(expected, false, 0, false, 0, true, latency));
  };

  // All eight believe they are at v0. All latency values are legal against
  // the default buffer (10000 ms covered >> 2*latency).
  std::vector<std::future<std::shared_ptr<AtomicUpdate::Response>>> futs;
  for (int i = 0; i < 8; ++i) {
    futs.push_back(std::async(std::launch::async, attempt, 0u,
                              static_cast<std::uint64_t>(10 + i)));
  }

  int ok_count = 0;
  int conflict_count = 0;
  std::uint64_t winner_latency = 0;
  for (auto& f : futs) {
    auto r = f.get();
    ASSERT_NE(r, nullptr);
    if (r->status == 0) {
      ++ok_count;
      winner_latency = r->allowed_latency_ms;
    } else if (r->status ==
               static_cast<std::uint8_t>(CommitStatus::VERSION_CONFLICT)) {
      ++conflict_count;
    } else {
      FAIL() << "unexpected status " << static_cast<int>(r->status) << ": "
             << r->message;
    }
  }
  EXPECT_EQ(ok_count, 1);
  EXPECT_EQ(conflict_count, 7);
  EXPECT_GE(winner_latency, 10u);
  EXPECT_LE(winner_latency, 17u);  // exactly one of the eight racing attempts

  // Losers refresh and retry serially against the advancing version: all 7
  // remaining updates eventually commit.
  for (int i = 0; i < 7; ++i) {
    Client c(srv.service_base());
    ASSERT_FALSE(c.construction_failed_);
    auto g = c.get();
    auto r = c.update(
        make_req(g->version, false, 0, false, 0, true,
                 200u + static_cast<std::uint64_t>(i)));
    ASSERT_NE(r, nullptr);
    ASSERT_EQ(r->status, 0u) << r->message;
  }
  Client final_cli(srv.service_base());
  ASSERT_FALSE(final_cli.construction_failed_);
  EXPECT_EQ(final_cli.get()->version, 8u);
  EXPECT_EQ(final_cli.get()->allowed_latency_ms, 206u);
}

TEST_F(NodeIntegration, PersistenceFailureReturnsErrorAndServiceSurvives) {
  Server srv;
  Client cli(srv.service_base());
  ASSERT_FALSE(cli.construction_failed_);
  ASSERT_EQ(cli.update(make_req(0, false, 0, true, 2000, false, 0))->status, 0u);

  sql_exec(srv.db,
           "CREATE TRIGGER ros_fail_insert BEFORE INSERT ON config_versions "
           "BEGIN SELECT RAISE(ABORT, 'injected disk outage'); END;");

  auto failed = cli.update(make_req(1, false, 0, false, 0, true, 40));
  ASSERT_NE(failed, nullptr);
  EXPECT_EQ(failed->status,
            static_cast<std::uint8_t>(CommitStatus::PERSISTENCE_FAILED));
  EXPECT_NE(failed->message.find("injected disk outage"), std::string::npos);
  EXPECT_EQ(failed->version, 1u);

  sql_exec(srv.db, "DROP TRIGGER ros_fail_insert;");
  auto recovered = cli.update(make_req(1, false, 0, false, 0, true, 40));
  ASSERT_NE(recovered, nullptr);
  EXPECT_EQ(recovered->status, 0u) << recovered->message;
  EXPECT_EQ(recovered->version, 2u);
}

TEST_F(NodeIntegration, RestartLoadsLastCommittedAndReportsCorruption) {
  std::string name = unique_node("persist");
  std::string dir = unique_dir();
  std::string db = dir + "/cfg.sqlite3";

  {
    rclcpp::NodeOptions opts;
    opts.arguments({"--ros-args", "-r", "__node:=" + name,
                    "-p", "db_path:=" + db});
    auto node = std::make_shared<RobotConfigNode>(opts);
    auto exec = std::make_shared<rclcpp::executors::MultiThreadedExecutor>(
        rclcpp::ExecutorOptions(), 4);
    exec->add_node(node);
    std::thread th([exec] { exec->spin(); });

    auto cli_node = std::make_shared<rclcpp::Node>(unique_node("cli2"));
    auto cli_exec =
        std::make_shared<rclcpp::executors::SingleThreadedExecutor>();
    cli_exec->add_node(cli_node);
    std::thread cth([cli_exec] { cli_exec->spin(); });
    auto c = cli_node->create_client<AtomicUpdate>("/" + name + "/update");
    ASSERT_TRUE(c->wait_for_service(15s));

    auto f1 = c->async_send_request(
        make_req(0, false, 0, true, 2000, false, 0));
    ASSERT_EQ(f1.wait_for(15s), std::future_status::ready);
    ASSERT_EQ(f1.get()->status, 0u);
    auto f2 = c->async_send_request(
        make_req(1, false, 0, false, 0, true, 30));
    ASSERT_EQ(f2.wait_for(15s), std::future_status::ready);
    ASSERT_EQ(f2.get()->status, 0u);

    exec->cancel();
    cli_exec->cancel();
    th.join();
    cth.join();
  }

  // Tamper the newest row out-of-band.
  sql_exec(db,
           "UPDATE config_versions SET allowed_latency_ms = 9999 "
           "WHERE version = 2;");

  {
    rclcpp::NodeOptions opts;
    opts.arguments({"--ros-args", "-r", "__node:=" + name,
                    "-p", "db_path:=" + db});
    auto node = std::make_shared<RobotConfigNode>(opts);
    const auto info = node->store().load_info();
    EXPECT_EQ(info.state, LoadState::RECOVERED);
    EXPECT_EQ(node->store().snapshot().version, 1u);
    EXPECT_NE(info.detail.find("version 2 rejected"), std::string::npos);

    auto exec = std::make_shared<rclcpp::executors::MultiThreadedExecutor>(
        rclcpp::ExecutorOptions(), 4);
    exec->add_node(node);
    std::thread th([exec] { exec->spin(); });

    auto cli_node = std::make_shared<rclcpp::Node>(unique_node("cli3"));
    auto cli_exec =
        std::make_shared<rclcpp::executors::SingleThreadedExecutor>();
    cli_exec->add_node(cli_node);
    std::thread cth([cli_exec] { cli_exec->spin(); });
    auto gc = cli_node->create_client<GetConfig>("/" + name + "/get");
    ASSERT_TRUE(gc->wait_for_service(15s));
    auto g = gc->async_send_request(std::make_shared<GetConfig::Request>());
    ASSERT_EQ(g.wait_for(15s), std::future_status::ready);
    auto resp = g.get();
    EXPECT_EQ(resp->version, 1u);
    EXPECT_EQ(resp->load_state,
              static_cast<std::uint8_t>(LoadState::RECOVERED));
    EXPECT_NE(resp->load_detail.find("SHA-256 mismatch"), std::string::npos);

    exec->cancel();
    cli_exec->cancel();
    th.join();
    cth.join();
  }
}

TEST_F(NodeIntegration, SnapshotTopicPublishesLatchedConfig) {
  Server srv;

  auto sub_node = std::make_shared<rclcpp::Node>(unique_node("sub"));
  auto sub_exec =
      std::make_shared<rclcpp::executors::SingleThreadedExecutor>();
  sub_exec->add_node(sub_node);
  std::thread sth([sub_exec] { sub_exec->spin(); });

  std::promise<ConfigSnapshotMsg> p;
  auto f = p.get_future();
  rclcpp::QoS qos(rclcpp::KeepLast(4));
  qos.transient_local().reliable();
  auto sub = sub_node->create_subscription<ConfigSnapshotMsg>(
      srv.service_base() + "/snapshots", qos,
      [&](const ConfigSnapshotMsg::SharedPtr msg) {
        if (msg->version >= 1) {
          p.set_value(*msg);
        }
      });

  Client cli(srv.service_base());
  ASSERT_FALSE(cli.construction_failed_);
  ASSERT_EQ(cli.update(make_req(0, true, 200.0, false, 0, false, 0))->status,
            0u);

  ASSERT_EQ(f.wait_for(10s), std::future_status::ready);
  auto m = f.get();
  EXPECT_EQ(m.version, 1u);
  EXPECT_DOUBLE_EQ(m.sample_rate_hz, 200.0);
  EXPECT_EQ(m.buffer_length, 1000u);

  sub_exec->cancel();
  sth.join();
}
