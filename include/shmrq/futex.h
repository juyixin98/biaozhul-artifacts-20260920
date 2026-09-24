// SPDX-License-Identifier: MIT
// Linux process-shared futex wrappers (CLOCK_MONOTONIC, absolute deadline).
#pragma once

#include <atomic>
#include <cstdint>

namespace shmrq {

enum class FutexRes { Woke, Timeout, Changed, Error };

// FUTEX_BITSET_MATCH_ANY (0xFFFFFFFF, interpreted as -1 by the syscall ABI).
inline constexpr int kFutexAllBits = static_cast<int>(0xFFFFFFFF);

// Process-shared wait: the futex word lives in a MAP_SHARED mapping.
// `observed` is the value the caller read before deciding to sleep; the
// kernel re-checks it atomically, closing the classic lost-wakeup race.
// deadline_ns is an absolute CLOCK_MONOTONIC instant (0 = never used).
FutexRes futex_wait_bitset(std::atomic<int>& word, int observed,
                           uint64_t deadline_ns, int bitset = kFutexAllBits) noexcept;

void futex_wake_bitset(std::atomic<int>& word, int count = 1,
                       int bitset = kFutexAllBits) noexcept;

} // namespace shmrq
