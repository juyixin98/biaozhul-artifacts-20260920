// SPDX-License-Identifier: Apache-2.0
//
// Integration tests: a real ConfigNode spun by a MultiThreadedExecutor and
// exercised over real rclcpp service clients (FastDDS loopback). These cover
// the end-to-end paths requested: legal-single-field-but-illegal-combination,
// commit from inside a callback, stale version, persistence failure and
// concurrent compare-and-swap.
#include <atomic>
#include <condition_variable>
#include <cstdio>
#include <cstdlib>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

#include <gtest/gtest.h>

#include <rclcpp/rclcpp.hpp>

#include "robot_param_atomic/config_node.hpp"
#include "robot_param_atomic/srv/get_config.hpp"
#include "robot_param_atomic/srv/update_config.hpp"

using robot_param_atomic::ConfigNode;
using robot_param_atomic::LoadStatus;
using robot_param_atomic::srv::GetConfig;
using robot_param_atomic::srv::UpdateConfig;

namespace
{

constexpr const char * kUpdateSrv = "robot_config/update";
constexpr const char * kGetSrv = "robot_config/get";

std::atomic<int> g_seq{0};

std::string unique_ns(const char * tag)
{
  return std::string("/it_") + tag + "_" + std::to_string(::getpid()) + "_" +
         std::to_string(g_seq.fetch_add(1));
}

std::string unique_db(const char * tag)
{
  return std::string("/tmp/it_") + tag + "_" + std::to_string(::getpid()) +
         "_" + std::to_string(g_seq.fetch_add(1)) + ".db";
}

// One self-contained ROS graph per test: a server node in its own namespace,
// a dedicated client node, and a background multi-threaded executor spinning
// ONLY the server. Client futures are pumped with explicit
// spin_until_future_complete so several test threads can each own an
// independent client without sharing executor state.
class NodeFixture
{
public:
  NodeFixture(
    const char * tag, bool probe_callback = false,
    const std::string & db_override = "")
  {
    ns_ = unique_ns(tag);
    if (db_override.empty()) {
      db_path_ = unique_db(tag);
      std::remove(db_path_.c_str());  // fresh temp database
    } else {
      db_path_ = db_override;  // reuse an existing database (restart tests)
    }

    rclcpp::NodeOptions opts;
    opts.arguments({"--ros-args", "-r", std::string("__ns:=") + ns_});
    opts.append_parameter_override("db_path", db_path_);
    opts.append_parameter_override("probe_callback_commit", probe_callback);
    server_ = std::make_shared<ConfigNode>(opts);

    rclcpp::NodeOptions copts;
    copts.arguments({"--ros-args", "-r", std::string("__ns:=") + ns_});
    client_node_ = std::make_shared<rclcpp::Node>("it_client", copts);
    update_cli_ = client_node_->create_client<UpdateConfig>(kUpdateSrv);
    get_cli_ = client_node_->create_client<GetConfig>(kGetSrv);

    executor_ = std::make_shared<rclcpp::executors::MultiThreadedExecutor>(
      rclcpp::ExecutorOptions(), 8);
    executor_->add_node(server_);
    spin_thread_ = std::thread([this]() { executor_->spin(); });
  }

  ~NodeFixture()
  {
    executor_->cancel();
    if (spin_thread_.joinable()) spin_thread_.join();
    executor_->remove_node(server_);
    server_.reset();
    client_node_.reset();
    executor_.reset();
  }

  bool wait_for_services()
  {
    return update_cli_->wait_for_service(std::chrono::seconds(5)) &&
           get_cli_->wait_for_service(std::chrono::seconds(5));
  }

  std::shared_ptr<UpdateConfig::Response> update(
    double expected, double rate, double cache, double latency)
  {
    auto req = std::make_shared<UpdateConfig::Request>();
    req->expected_version = expected;
    req->sampling_rate_hz = rate;
    req->cache_length_s = cache;
    req->allowed_latency_s = latency;
    auto fut = update_cli_->async_send_request(req);
    if (
      rclcpp::spin_until_future_complete(
        client_node_, fut, std::chrono::seconds(5)) !=
      rclcpp::FutureReturnCode::SUCCESS) {
      return nullptr;
    }
    return fut.get();
  }

  std::shared_ptr<GetConfig::Response> get()
  {
    auto req = std::make_shared<GetConfig::Request>();
    auto fut = get_cli_->async_send_request(req);
    if (
      rclcpp::spin_until_future_complete(
        client_node_, fut, std::chrono::seconds(5)) !=
      rclcpp::FutureReturnCode::SUCCESS) {
      return nullptr;
    }
    return fut.get();
  }

  std::shared_ptr<ConfigNode> server() { return server_; }
  const std::string & ns() const { return ns_; }
  const std::string & db_path() const { return db_path_; }

private:
  std::string ns_;
  std::string db_path_;
  std::shared_ptr<ConfigNode> server_;
  std::shared_ptr<rclcpp::Node> client_node_;
  rclcpp::Client<UpdateConfig>::SharedPtr update_cli_;
  rclcpp::Client<GetConfig>::SharedPtr get_cli_;
  std::shared_ptr<rclcpp::executors::MultiThreadedExecutor> executor_;
  std::thread spin_thread_;
};

// Synchronous update through a caller-owned client node (used by racing
// threads so none share executor / node spin state).
std::shared_ptr<UpdateConfig::Response> update_on_own_node(
  const std::string & ns, double expected, double rate, double cache,
  double latency)
{
  rclcpp::NodeOptions opts;
  opts.arguments({"--ros-args", "-r", std::string("__ns:=") + ns});
  auto node = std::make_shared<rclcpp::Node>(
    "it_racer_" + std::to_string(g_seq.fetch_add(1)), opts);
  auto cli = node->create_client<UpdateConfig>(kUpdateSrv);
  if (!cli->wait_for_service(std::chrono::seconds(5))) {
    return nullptr;
  }
  auto req = std::make_shared<UpdateConfig::Request>();
  req->expected_version = expected;
  req->sampling_rate_hz = rate;
  req->cache_length_s = cache;
  req->allowed_latency_s = latency;
  auto fut = cli->async_send_request(req);
  if (
    rclcpp::spin_until_future_complete(node, fut, std::chrono::seconds(8)) !=
    rclcpp::FutureReturnCode::SUCCESS) {
    return nullptr;
  }
  return fut.get();
}

class Integration : public ::testing::Test
{
public:
  static void SetUpTestSuite()
  {
    if (!rclcpp::ok()) {
      rclcpp::init(0, nullptr);
    }
  }
};

// ---------------------------------------------------------------------------

TEST_F(Integration, StartFreshThenReadDefaults)
{
  NodeFixture f("fresh");
  ASSERT_TRUE(f.wait_for_services());
  EXPECT_EQ(f.server()->load_outcome().status, LoadStatus::FRESH);
  auto g = f.get();
  ASSERT_NE(g, nullptr);
  EXPECT_DOUBLE_EQ(g->version, 0.0);
  EXPECT_DOUBLE_EQ(g->sampling_rate_hz, 100.0);
  EXPECT_DOUBLE_EQ(g->cache_length_s, 1.0);
  EXPECT_DOUBLE_EQ(g->allowed_latency_s, 0.25);
  EXPECT_EQ(g->config_hash.size(), 64u);  // real SHA-256 hex digest
}

TEST_F(Integration, ValidCommitIsAtomic)
{
  NodeFixture f("valid");
  ASSERT_TRUE(f.wait_for_services());
  auto r = f.update(0.0, 200, 2.0, 0.5);
  ASSERT_NE(r, nullptr);
  EXPECT_TRUE(r->ok);
  EXPECT_EQ(r->code, UpdateConfig::Response::OK);
  EXPECT_DOUBLE_EQ(r->version, 1.0);
  EXPECT_DOUBLE_EQ(r->cache_length_s, 2.0);

  auto g = f.get();
  ASSERT_NE(g, nullptr);
  EXPECT_DOUBLE_EQ(g->version, 1.0);
  EXPECT_EQ(g->commit_seq, 1);
}

// Each field individually legal, but cache 0.4 < 2*latency 0.3 -> whole
// batch rejected, nothing changes.
TEST_F(Integration, SingleFieldsLegalButCombinationIllegal)
{
  NodeFixture f("combo");
  ASSERT_TRUE(f.wait_for_services());
  auto r = f.update(0.0, 1000, 0.4, 0.3);
  ASSERT_NE(r, nullptr);
  EXPECT_FALSE(r->ok);
  EXPECT_EQ(r->code, UpdateConfig::Response::REJECT_CONSTRAINT);

  auto g = f.get();
  ASSERT_NE(g, nullptr);
  EXPECT_DOUBLE_EQ(g->version, 0.0);
  EXPECT_DOUBLE_EQ(g->sampling_rate_hz, 100.0);  // defaults intact
  EXPECT_DOUBLE_EQ(g->cache_length_s, 1.0);
  EXPECT_DOUBLE_EQ(g->allowed_latency_s, 0.25);
}

TEST_F(Integration, StaleVersionRejectedThenFreshVersionSucceeds)
{
  NodeFixture f("stale");
  ASSERT_TRUE(f.wait_for_services());
  ASSERT_TRUE(f.update(0.0, 200, 2, 0.5)->ok);

  auto stale = f.update(0.0, 300, 4, 1.0);
  ASSERT_NE(stale, nullptr);
  EXPECT_FALSE(stale->ok);
  EXPECT_EQ(stale->code, UpdateConfig::Response::REJECT_STALE_VERSION);
  EXPECT_DOUBLE_EQ(stale->version, 1.0);  // server reports current version

  auto fresh = f.update(1.0, 300, 4, 1.0);
  ASSERT_NE(fresh, nullptr);
  EXPECT_TRUE(fresh->ok);
  EXPECT_DOUBLE_EQ(fresh->version, 2.0);
}

// A commit attempted from inside a change callback is rejected with a distinct
// code, does not dead-lock and leaves the version at the committed value.
TEST_F(Integration, CommitInsideCallbackIsRejected)
{
  NodeFixture f("callback", /*probe_callback=*/true);
  ASSERT_TRUE(f.wait_for_services());

  auto r = f.update(0.0, 50, 2.0, 0.5);
  ASSERT_NE(r, nullptr);
  EXPECT_TRUE(r->ok);
  EXPECT_DOUBLE_EQ(r->version, 1.0);

  EXPECT_EQ(
    f.server()->last_callback_probe_code(),
    static_cast<int>(UpdateConfig::Response::REJECT_UPDATE_IN_CALLBACK));
  auto g = f.get();
  ASSERT_NE(g, nullptr);
  EXPECT_DOUBLE_EQ(g->version, 1.0);  // nested commit changed nothing
}

// Persistence failure: the injected storage error makes the service report
// ERR_PERSISTENCE and the configuration stays at the previous snapshot.
TEST_F(Integration, PersistenceFailureRollsBack)
{
  NodeFixture f("persist_fail");
  ASSERT_TRUE(f.wait_for_services());

  setenv("PARAM_ATOMIC_FAULT", "commit_io", 1);
  auto r = f.update(0.0, 444, 2, 0.5);
  unsetenv("PARAM_ATOMIC_FAULT");

  ASSERT_NE(r, nullptr);
  EXPECT_FALSE(r->ok);
  EXPECT_EQ(r->code, UpdateConfig::Response::ERR_PERSISTENCE);

  auto g = f.get();
  ASSERT_NE(g, nullptr);
  EXPECT_DOUBLE_EQ(g->version, 0.0);
  EXPECT_DOUBLE_EQ(g->sampling_rate_hz, 100.0);
}

// N independent clients race the same expected_version: exactly one wins, the
// rest get REJECT_STALE_VERSION, and the final version increments once.
TEST_F(Integration, ConcurrentCasExactlyOneWinner)
{
  NodeFixture f("race");
  ASSERT_TRUE(f.wait_for_services());

  constexpr int kN = 10;
  std::vector<std::thread> threads;
  std::atomic<int> wins{0}, stale{0}, other{0};
  std::mutex m;
  bool go = false;
  std::condition_variable cv;

  for (int i = 0; i < kN; ++i) {
    threads.emplace_back(
      [&, i]() {
        std::unique_lock<std::mutex> lk(m);
        cv.wait(lk, [&] { return go; });
        lk.unlock();
        // Each thread uses its own node+client, like N separate processes.
        auto resp = update_on_own_node(
          f.ns(), 0.0, 100 + i, 2.0 + i * 0.5, 0.5);
        if (!resp) {
          other++;
          return;
        }
        if (resp->ok) wins++;
        else if (resp->code == UpdateConfig::Response::REJECT_STALE_VERSION)
          stale++;
        else other++;
      });
  }
  {
    std::lock_guard<std::mutex> lk(m);
    go = true;
  }
  cv.notify_all();
  for (auto & t : threads) t.join();

  EXPECT_EQ(wins.load(), 1);
  EXPECT_EQ(stale.load(), kN - 1);
  EXPECT_EQ(other.load(), 0);
  auto g = f.get();
  ASSERT_NE(g, nullptr);
  EXPECT_DOUBLE_EQ(g->version, 1.0);
}

// Direct ROS parameter writes are refused: the parameters are a read-only
// mirror of the committed snapshot.
TEST_F(Integration, DirectParameterSetIsRefused)
{
  NodeFixture f("param_ro");
  ASSERT_TRUE(f.wait_for_services());
  std::vector<rclcpp::Parameter> params{
    rclcpp::Parameter("sampling_rate_hz", 999.0)};
  auto results = f.server()->set_parameters(params);
  ASSERT_EQ(results.size(), 1u);
  EXPECT_FALSE(results[0].successful);
  EXPECT_NE(results[0].reason.find("atomic config service"), std::string::npos);
}

// After commits, a restart loads the latest committed configuration.
TEST_F(Integration, RestartLoadsLatestCommitted)
{
  std::string db;
  {
    NodeFixture f("restart");
    ASSERT_TRUE(f.wait_for_services());
    ASSERT_TRUE(f.update(0.0, 250, 3.0, 1.0)->ok);
    ASSERT_TRUE(f.update(1.0, 260, 4.0, 1.5)->ok);
    db = f.db_path();
  }
  {
    NodeFixture f2("restart2", false, db);
    ASSERT_TRUE(f2.wait_for_services());
    EXPECT_EQ(f2.server()->load_outcome().status, LoadStatus::LOADED);
    auto g = f2.get();
    ASSERT_NE(g, nullptr);
    EXPECT_DOUBLE_EQ(g->version, 2.0);
    EXPECT_DOUBLE_EQ(g->sampling_rate_hz, 260.0);
    EXPECT_DOUBLE_EQ(g->cache_length_s, 4.0);
    EXPECT_DOUBLE_EQ(g->allowed_latency_s, 1.5);
  }
}

}  // namespace
