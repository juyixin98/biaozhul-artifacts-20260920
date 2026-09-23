// Cooperative cancellation. SIGINT/SIGTERM set a global atomic flag; the
// optimizer polls it (and Ceres iteration callbacks poll it) so a Ctrl-C
// aborts the solve cleanly. No half-optimized result is ever published.
#ifndef PGO_SIGNAL_HPP
#define PGO_SIGNAL_HPP

#include <atomic>
#include <cstdint>

namespace pgo {

class Canceller {
 public:
  static Canceller& instance();

  void installHandlers();
  void requestCancel(const char* reason);
  bool cancelled() const { return flag_.load(std::memory_order_acquire); }
  const char* reason() const { return reason_; }
  void reset();

 private:
  Canceller() = default;
  std::atomic<int> flag_{0};
  const char* reason_ = "";
};

// Test hook: request cancellation after a fixed wall-clock delay (ms).
// Returns immediately; the timer runs on a detached thread. Cancelled at
// program exit harmlessly.
void scheduleCancelAfterMs(uint64_t ms);

}  // namespace pgo

#endif
