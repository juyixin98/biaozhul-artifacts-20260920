#include "signal.hpp"

#include <chrono>
#include <csignal>
#include <thread>

namespace pgo {

Canceller& Canceller::instance() {
  static Canceller c;
  return c;
}

namespace {
std::atomic<int>* flagForHandler = nullptr;

void signalHandler(int sig) {
  (void)sig;
  // async-signal-safe: only an atomic release-store.
  if (flagForHandler) flagForHandler->store(1, std::memory_order_release);
}
}  // namespace

void Canceller::installHandlers() {
  flagForHandler = &flag_;
  struct sigaction sa {};
  sa.sa_handler = signalHandler;
  sigemptyset(&sa.sa_mask);
  sa.sa_flags = 0;
  sigaction(SIGINT, &sa, nullptr);
  sigaction(SIGTERM, &sa, nullptr);
}

void Canceller::requestCancel(const char* reason) {
  flag_.store(1, std::memory_order_release);
  reason_ = reason;
}

void Canceller::reset() {
  flag_.store(0, std::memory_order_release);
  reason_ = "";
}

void scheduleCancelAfterMs(uint64_t ms) {
  std::thread([ms]() {
    std::this_thread::sleep_for(std::chrono::milliseconds(ms));
    Canceller::instance().requestCancel("cancel-after-ms timer");
  }).detach();
}

}  // namespace pgo
