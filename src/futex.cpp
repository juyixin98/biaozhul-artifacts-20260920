// SPDX-License-Identifier: MIT
// Linux process-shared futex wrappers (CLOCK_MONOTONIC, absolute deadline).
#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif
#include "shmrq/futex.h"

#include <cerrno>
#include <ctime>
#include <linux/futex.h>
#include <sys/syscall.h>
#include <unistd.h>

namespace shmrq {

static long futex_sys(int* uaddr, int op, int val,
                      const timespec* ts, int bitset) noexcept {
  return syscall(SYS_futex, uaddr, op, val, ts, nullptr, bitset);
}

FutexRes futex_wait_bitset(std::atomic<int>& word, int observed,
                           uint64_t deadline_ns, int bitset) noexcept {
  timespec ts;
  ts.tv_sec = static_cast<time_t>(deadline_ns / 1'000'000'000ull);
  ts.tv_nsec = static_cast<long>(deadline_ns % 1'000'000'000ull);

  int* addr = reinterpret_cast<int*>(
      static_cast<void*>(const_cast<std::atomic<int>*>(&word)));

  while (true) {
    // FUTEX_WAIT_BITSET without FUTEX_PRIVATE_FLAG => process-shared.
    // Absolute timeout, CLOCK_MONOTONIC.  The kernel compares the word to
    // `observed` atomically with enqueue, so a wake cannot be lost.
    long r = futex_sys(addr, FUTEX_WAIT_BITSET, observed, &ts, bitset);
    if (r == 0)
      return FutexRes::Woke;
    if (errno == EINTR)
      continue; // absolute deadline stays valid
    if (errno == ETIMEDOUT)
      return FutexRes::Timeout;
    if (errno == EAGAIN || errno == EWOULDBLOCK)
      return FutexRes::Changed;
    return FutexRes::Error;
  }
}

void futex_wake_bitset(std::atomic<int>& word, int count, int bitset) noexcept {
  int* addr = reinterpret_cast<int*>(
      static_cast<void*>(const_cast<std::atomic<int>*>(&word)));
  futex_sys(addr, FUTEX_WAKE_BITSET, count, nullptr, bitset);
}

} // namespace shmrq
