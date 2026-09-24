// SPDX-License-Identifier: MIT
// Unit tests: in-process ring logic, high-frequency ring-index wrap,
// torn-write recovery, CRC corruption, a pthread SPSC smoke run, and a
// real two-process (fork + MAP_SHARED) pass — the cross-process case is
// what exercises process-shared atomics and process-shared futexes.
#include <sys/mman.h>
#include <sys/wait.h>
#include <unistd.h>

#include <atomic>
#include <cstdio>
#include <cstring>
#include <string>
#include <thread>
#include <vector>

#include "shmrq/ring.h"

using namespace shmrq;

namespace {
int g_fail = 0;
int g_tests = 0;
#define CHECK(cond)                                                            \
  do {                                                                         \
    ++g_tests;                                                                 \
    if (!(cond)) {                                                             \
      ++g_fail;                                                                \
      std::fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond);     \
    }                                                                          \
  } while (0)

struct Mem {
  size_t bytes;
  void* base;
  explicit Mem(const Config& c) : bytes(Ring::bytes_for(c)), base(nullptr) {
    base = mmap(nullptr, bytes, PROT_READ | PROT_WRITE,
                MAP_SHARED | MAP_ANONYMOUS, -1, 0);
    if (base == MAP_FAILED)
      base = nullptr;
  }
  ~Mem() {
    if (base)
      munmap(base, bytes);
  }
};

// CRC-32/ISO-HDLC known-answer: crc32("123456789") == 0xCBF43926
void test_crc() {
  const char* s = "123456789";
  CHECK(crc32_ieee(s, 9) == 0xCBF43926u);
  CHECK(crc32_ieee("", 0) == 0x00000000u);
  CHECK(crc32_ieee("a", 1) == 0xE8B7BE43u);
  CHECK(Ring::crc_record(1, s, 9) == Ring::crc_record(1, s, 9));
  CHECK(Ring::crc_record(1, s, 9) != Ring::crc_record(2, s, 9));
  // Empty records still have a domain tag (seq+len participate).
  CHECK(Ring::crc_record(0, nullptr, 0) != Ring::crc_record(1, nullptr, 0));
  // A payload valid under one position fails under another (domain binding).
  CHECK(Ring::crc_record(0, s, 9) != Ring::crc_record(5, s, 9));
}

void test_basic_pingpong() {
  Config c{4, 128};
  Mem m(c);
  CHECK(m.base);
  CHECK(Ring::create(m.base, m.bytes, c) == Err::Ok);
  {
    Ring p(m.base, m.bytes, Role::Producer);
    RecoverReport r;
    CHECK(p.open(&r) == Err::Ok);
    Ring co(m.base, m.bytes, Role::Consumer);
    CHECK(co.open() == Err::Ok);

    for (uint64_t i = 0; i < 4; ++i) {
      std::string b = "msg-" + std::to_string(i);
      CHECK(p.publish(b.data(), static_cast<uint32_t>(b.size()), 0) == Err::Ok);
    }
    // 5th publish on a cap-4 queue must reject non-blocking.
    CHECK(p.publish("x", 1, 0) == Err::Full);

    for (uint64_t i = 0; i < 4; ++i) {
      Ring::Message msg;
      CHECK(co.consume(msg, 0) == Err::Ok);
      CHECK(msg.pos == i);
      CHECK(co.verify_message(msg) == Err::Ok);
      std::string got(reinterpret_cast<const char*>(msg.payload), msg.len);
      CHECK(got == "msg-" + std::to_string(i));
      CHECK(co.release(msg) == Err::Ok);
    }
    Ring::Message msg;
    CHECK(co.consume(msg, 0) == Err::Empty);
  }
}

void test_wrap_and_wraparound_counter() {
  Config c{4, 64};
  Mem m(c);
  CHECK(Ring::create(m.base, m.bytes, c) == Err::Ok);
  Ring p(m.base, m.bytes, Role::Producer);
  CHECK(p.open() == Err::Ok);
  Ring co(m.base, m.bytes, Role::Consumer);
  CHECK(co.open() == Err::Ok);

  const uint64_t N = 4097; // > 1024 ring cycles; odd number exercises index 0
  for (uint64_t i = 0; i < N; ++i) {
    Ring::Slot s;
    CHECK(p.reserve(s, 0) == Err::Ok);
    CHECK(s.index == static_cast<uint32_t>(i & 3));
    char b[16];
    int n = std::snprintf(b, sizeof(b), "%llu",
                          static_cast<unsigned long long>(i));
    std::memcpy(s.payload, b, static_cast<size_t>(n));
    CHECK(p.commit(s, static_cast<uint32_t>(n),
                   Ring::crc_record(i, b, static_cast<uint32_t>(n))) ==
          Err::Ok);

    Ring::Message msg;
    CHECK(co.consume(msg, 0) == Err::Ok);
    CHECK(msg.pos == i);
    CHECK(co.verify_message(msg) == Err::Ok);
    CHECK(std::memcmp(msg.payload, b, static_cast<size_t>(n)) == 0);
    CHECK(co.release(msg) == Err::Ok);
  }
  Stats st = p.stats();
  CHECK(st.head == N);
  CHECK(st.tail == N);
  CHECK(st.wraps_head == N / 4);
  CHECK(st.wraps_tail == N / 4);
}

void test_torn_write_recovery() {
  Config c{4, 64};
  Mem m(c);
  CHECK(Ring::create(m.base, m.bytes, c) == Err::Ok);
  // First producer lifetime: publish 2 committed, then leave a torn write.
  {
    Ring p(m.base, m.bytes, Role::Producer);
    CHECK(p.open() == Err::Ok);
    CHECK(p.publish("a", 1, 0) == Err::Ok);
    CHECK(p.publish("bb", 2, 0) == Err::Ok);

    Ring::Slot s;
    CHECK(p.reserve(s, 0) == Err::Ok);
    CHECK(s.pos == 2);
    std::memcpy(s.payload, "torn", 4);
    // "process killed": no commit, mapping persists.
  }
  {
    Ring p2(m.base, m.bytes, Role::Producer);
    RecoverReport r;
    CHECK(p2.open(&r) == Err::Ok);
    CHECK(r.head_loaded == 2);
    CHECK(r.adopted == 1);
    CHECK(r.in_flight == 2);
    // The two confirmed messages are untouched; the torn slot is reusable.
    CHECK(p2.publish("cc", 2, 0) == Err::Ok);
    Ring co(m.base, m.bytes, Role::Consumer);
    CHECK(co.open() == Err::Ok);
    const char* want[] = {"a", "bb", "cc"};
    for (int i = 0; i < 3; ++i) {
      Ring::Message msg;
      CHECK(co.consume(msg, 0) == Err::Ok);
      CHECK(msg.pos == static_cast<uint64_t>(i));
      CHECK(co.verify_message(msg) == Err::Ok);
      CHECK(msg.len == std::strlen(want[i]));
      CHECK(std::memcmp(msg.payload, want[i], msg.len) == 0);
      CHECK(co.release(msg) == Err::Ok);
    }
  }
}

void test_committed_after_crash_is_preserved() {
  Config c{4, 64};
  Mem m(c);
  CHECK(Ring::create(m.base, m.bytes, c) == Err::Ok);
  {
    Ring p(m.base, m.bytes, Role::Producer);
    CHECK(p.open() == Err::Ok);
    for (int i = 0; i < 4; ++i) {
      std::string b = "keep-" + std::to_string(i);
      CHECK(p.publish(b.data(), static_cast<uint32_t>(b.size()), 0) ==
            Err::Ok);
    }
    // Producer dies with the full queue of committed records.
  }
  Ring p2(m.base, m.bytes, Role::Producer);
  RecoverReport r;
  CHECK(p2.open(&r) == Err::Ok);
  CHECK(r.in_flight == 4);
  CHECK(r.adopted == 0);
}

void test_corruption_demands_reinit() {
  Config c{4, 64};
  Mem m(c);
  CHECK(Ring::create(m.base, m.bytes, c) == Err::Ok);
  Ring p(m.base, m.bytes, Role::Producer);
  CHECK(p.open() == Err::Ok);
  CHECK(p.publish("abcdef", 6, 0) == Err::Ok);
  // Flip a byte of a committed, unconsumed record.
  CHECK(p.debug_flip_payload(0) == Err::Ok);
  Ring p2(m.base, m.bytes, Role::Producer);
  CHECK(p2.open() == Err::ReinitRequired);
}

void test_blocking_timeout() {
  // Dedicated queue with NO consumer ever attached: nothing bumps the
  // producer/consumer wake words, so the timed publish must sleep on the
  // real futex right up to its deadline.
  Config c{2, 64};
  Mem m(c);
  CHECK(Ring::create(m.base, m.bytes, c) == Err::Ok);
  Ring p(m.base, m.bytes, Role::Producer);
  CHECK(p.open() == Err::Ok);
  CHECK(p.publish("a", 1, 0) == Err::Ok);
  CHECK(p.publish("b", 1, 0) == Err::Ok);
  timespec t0{}, t1{};
  clock_gettime(CLOCK_MONOTONIC, &t0);
  uint64_t dl = (uint64_t)t0.tv_sec * 1000000000ull + t0.tv_nsec +
                30 * 1000000ull;
  CHECK(p.publish("c", 1, dl) == Err::Full);
  clock_gettime(CLOCK_MONOTONIC, &t1);
  int64_t ns = (int64_t)(t1.tv_sec - t0.tv_sec) * 1000000000ll +
               (int64_t)t1.tv_nsec - (int64_t)t0.tv_nsec;
  int64_t ms = ns / 1000000;
  CHECK(ms >= 25 && ms < 500);
}

void test_thread_spsc() {
  // Sanity only: thread sharing proves the lock-free types work at all;
  // it is NOT evidence for cross-process memory ordering (different
  // mapping, different cache-coherence path through shm + futex).
  Config c{8, 256};
  Mem m(c);
  CHECK(Ring::create(m.base, m.bytes, c) == Err::Ok);
  const uint64_t N = 200000;
  std::atomic<bool> done{false};
  std::thread cons([&] {
    Ring co(m.base, m.bytes, Role::Consumer);
    if (co.open() != Err::Ok) { g_fail++; return; }
    for (uint64_t i = 0; i < N; ++i) {
      Ring::Message msg;
      while (co.consume(msg, 0) != Err::Ok) {
        timespec sl{0, 1000};
        nanosleep(&sl, nullptr);
      }
      if (msg.pos != i || co.verify_message(msg) != Err::Ok) {
        ++g_fail;
        break;
      }
      if (co.release(msg) != Err::Ok) { ++g_fail; break; }
    }
    done = true;
  });
  std::thread prod([&] {
    Ring p(m.base, m.bytes, Role::Producer);
    if (p.open() != Err::Ok) { g_fail++; return; }
    for (uint64_t i = 0; i < N; ++i) {
      while (p.publish(&i, sizeof(i), 0) == Err::Full) {
        timespec sl{0, 1000};
        nanosleep(&sl, nullptr);
      }
    }
  });
  prod.join();
  cons.join();
  CHECK(done.load());
}

void test_consumer_crash_between_free_and_ledger() {
  // release() frees the slot (seq := p+cap) and then ledgers tail.  Crash
  // in the gap leaves tail lagging.  A new consumer must not declare
  // corruption: the freed slot is legitimately reusable, and once the
  // producer wraps it, the consumer simply resumes at head.
  Config c{4, 64};
  Mem m(c);
  CHECK(Ring::create(m.base, m.bytes, c) == Err::Ok);
  {
    Ring p(m.base, m.bytes, Role::Producer);
    CHECK(p.open() == Err::Ok);
    Ring co(m.base, m.bytes, Role::Consumer);
    CHECK(co.open() == Err::Ok);
    for (uint64_t i = 0; i < 4; ++i) {
      std::string b = "x" + std::to_string(i);
      CHECK(p.publish(b.data(), static_cast<uint32_t>(b.size()), 0) == Err::Ok);
    }
    for (uint64_t i = 0; i < 4; ++i) {
      Ring::Message msg;
      CHECK(co.consume(msg, 0) == Err::Ok);
      CHECK(co.release(msg) == Err::Ok);
    }
    // Revert to the exact crash-window state: slots 2 and 3 freed
    // (seq == p+cap, as release wrote) but the tail ledger stuck at 2.
    auto* h = static_cast<Header*>(m.base);
    uint32_t sb = static_cast<uint32_t>(
        (Ring::bytes_for(c) - kHeaderBytes) / c.capacity);
    auto sets = [&](uint32_t slot_idx, uint64_t val) {
      auto* sh = reinterpret_cast<SlotHdr*>(
          static_cast<uint8_t*>(m.base) + size_t(kHeaderBytes) + size_t(slot_idx) * sb);
      sh->seq.store(val, std::memory_order_release);
    };
    sets(2, 2 + 4); // freed value for position 2
    sets(3, 3 + 4); // freed value for position 3
    h->tail.store(2, std::memory_order_release);
  }
  // Producer advances and reuses slots 2,3 for positions 6,7.
  {
    Ring p2(m.base, m.bytes, Role::Producer);
    RecoverReport r;
    CHECK(p2.open(&r) == Err::Ok);
    for (uint64_t i = 4; i < 8; ++i)
      CHECK(p2.publish("z", 1, 0) == Err::Ok);
    // New consumer: tail hint says 2, but positions 2,3's slots were
    // already freed and are now reused (6,7). Positions 4,5 then 6,7
    // remain to be read; positions 2,3 are past history. It must reach
    // the current frontier without a ReinitRequired error.
    Ring co3(m.base, m.bytes, Role::Consumer);
    CHECK(co3.open() == Err::Ok);
    uint64_t seen = 0;
    for (;;) {
      Ring::Message msg;
      Err e = co3.consume(msg, 0);
      if (e == Err::Empty)
        break;
      CHECK(e == Err::Ok);
      CHECK(co3.verify_message(msg) == Err::Ok);
      CHECK(co3.release(msg) == Err::Ok);
      ++seen;
    }
    CHECK(seen == 4); // positions 4..7
    CHECK(co3.tail_pos() == 8);
  }
}

// Real cross-process pass over a MAP_SHARED anonymous region inherited by
// fork().  After fork, parent and child have distinct page tables mapping
// the same physical pages — the actual cross-process contract.
void test_two_processes() {
  Config c{8, 256};
  Mem m(c);
  CHECK(m.base);
  CHECK(Ring::create(m.base, m.bytes, c) == Err::Ok);
  const uint64_t N = 100000;
  pid_t pid = fork();
  CHECK(pid >= 0);
  if (pid == 0) {
    // Child: consumer
    Ring co(m.base, m.bytes, Role::Consumer);
    if (co.open() != Err::Ok)
      _exit(10);
    timespec idle_start{};
    bool idling = false;
    for (uint64_t i = 0; i < N; ++i) {
      Ring::Message msg;
      Err e = co.consume(msg, 0);
      while (e == Err::Empty) {
        if (!idling) {
          clock_gettime(CLOCK_MONOTONIC, &idle_start);
          idling = true;
        }
        // Block on the real process-shared futex (short deadline keeps
        // the loop EINTR-safe); a genuinely stuck producer (no record for
        // 30s straight) fails the test, but sporadic scheduler pauses do
        // not accumulate across messages.
        timespec ts{};
        clock_gettime(CLOCK_MONOTONIC, &ts);
        uint64_t dl = (uint64_t)ts.tv_sec * 1000000000ull + ts.tv_nsec +
                      100000000ull;
        e = co.consume(msg, dl);
        timespec now{};
        clock_gettime(CLOCK_MONOTONIC, &now);
        int64_t idle_ms =
            (int64_t)(now.tv_sec - idle_start.tv_sec) * 1000 +
            (int64_t)(now.tv_nsec - idle_start.tv_nsec) / 1000000;
        if (idle_ms > 5000)
          _exit(13); // genuinely stuck (no new record for 5s straight)
      }
      idling = false;
      if (e != Err::Ok && e != Err::Empty)
        _exit(11);
      uint64_t v = 0;
      std::memcpy(&v, msg.payload, sizeof(v));
      if (msg.pos != i || v != i * 3 + 1 ||
          co.verify_message(msg) != Err::Ok)
        _exit(20);
      if (co.release(msg) != Err::Ok)
        _exit(12);
    }
    _exit(0);
  }
  // Parent: producer
  {
    Ring p(m.base, m.bytes, Role::Producer);
    CHECK(p.open() == Err::Ok);
    uint64_t produced = 0;
    int child_dead = 0;
    timespec full_start{};
    bool full_idle = false;
    while (produced < N && !child_dead) {
      uint64_t v = produced * 3 + 1;
      Err e = p.publish(&v, sizeof(v), 0);
      while (e == Err::Full) {
        if (!full_idle) {
          clock_gettime(CLOCK_MONOTONIC, &full_start);
          full_idle = true;
        }
        timespec ts{};
        clock_gettime(CLOCK_MONOTONIC, &ts);
        uint64_t dl = (uint64_t)ts.tv_sec * 1000000000ull + ts.tv_nsec +
                      100000000ull; // 100ms wait, then re-check child
        e = p.publish(&v, sizeof(v), dl);
        int wstatus = 0;
        pid_t r = waitpid(pid, &wstatus, WNOHANG);
        if (r == pid) {
          child_dead = 1;
          CHECK(WIFEXITED(wstatus));
          CHECK(WEXITSTATUS(wstatus) == 0);
          break;
        }
        timespec now{};
        clock_gettime(CLOCK_MONOTONIC, &now);
        int64_t idle_ms =
            (int64_t)(now.tv_sec - full_start.tv_sec) * 1000 +
            (int64_t)(now.tv_nsec - full_start.tv_nsec) / 1000000;
        if (idle_ms > 5000) {
          CHECK(!"producer stuck >5s with no slot released");
          child_dead = 1;
        }
      }
      full_idle = false;
      if (e == Err::Ok)
        ++produced;
    }
    CHECK(produced == N);
  }
  int status = 0;
  waitpid(pid, &status, 0);
  CHECK(WIFEXITED(status));
  CHECK(WEXITSTATUS(status) == 0);
}

} // namespace

int main() {
  static_assert(std::atomic<uint64_t>::is_always_lock_free &&
                    std::atomic<uint32_t>::is_always_lock_free &&
                    std::atomic<int>::is_always_lock_free,
                "process-shared atomics must be lock free");
  CHECK(std::atomic<uint64_t>::is_always_lock_free);
  test_crc();
  test_basic_pingpong();
  test_wrap_and_wraparound_counter();
  test_torn_write_recovery();
  test_committed_after_crash_is_preserved();
  test_corruption_demands_reinit();
  test_consumer_crash_between_free_and_ledger();
  test_blocking_timeout();
  test_thread_spsc();
  test_two_processes();
  std::printf("unit tests: %d checks, %d failures\n", g_tests, g_fail);
  return g_fail ? 1 : 0;
}
