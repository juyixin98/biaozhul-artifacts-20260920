// SPDX-License-Identifier: MIT
// shmrq — operator/test CLI for the shared-memory SPSC ring.
#include <algorithm>
#include <cerrno>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fcntl.h>
#include <signal.h>
#include <string>
#include <thread>
#include <vector>

#include "shmrq/ring.h"
#include "shmrq/shm.h"

using namespace shmrq;

namespace {

constexpr int EXIT_GATE_LOCKED = 2;
constexpr int EXIT_INTEGRITY = 3;
constexpr int EXIT_EMPTY_TIMEOUT = 4;
constexpr int EXIT_FULL_TIMEOUT = 5;
constexpr int EXIT_USAGE = 6;
constexpr int EXIT_REINIT = 7;

volatile sig_atomic_t g_stop = 0;
void on_signal(int) { g_stop = 1; }

struct Args {
  const char* name = nullptr;
  uint32_t capacity = kDefaultCapacity;
  uint32_t max_payload = kDefaultMaxPayload;
  long long count = 1;             // -1 = unbounded
  int size = 64;                   // synthetic payload body size
  int size_range = 0;              // 0 => fixed size; else [size, size+range)
  uint64_t timeout_ms = 0;         // producer full / consumer idle timeout
  uint64_t pace_us = 0;
  long long fault_at = -1;         // stall (and optionally exit) at this seq
  uint64_t fault_ms = 1000;
  bool fault_exit = false;
  bool verify_content = true;
  const char* in_file = nullptr;
  bool quiet = false;
};

bool parse_u32(const char* s, uint32_t& out) {
  char* end = nullptr;
  errno = 0;
  unsigned long v = strtoul(s, &end, 10);
  if (errno || !end || *end || v > 0xFFFFFFFFull) return false;
  out = static_cast<uint32_t>(v);
  return true;
}
bool parse_ll(const char* s, long long& out) {
  char* end = nullptr;
  errno = 0;
  long long v = strtoll(s, &end, 10);
  if (errno || !end || *end) return false;
  out = v;
  return true;
}
bool parse_u64(const char* s, uint64_t& out) {
  char* end = nullptr;
  errno = 0;
  unsigned long long v = strtoull(s, &end, 10);
  if (errno || !end || *end) return false;
  out = static_cast<uint64_t>(v);
  return true;
}

uint64_t deadline_from_ms(uint64_t ms) {
  if (ms == 0)
    return 0;
  timespec ts{};
  clock_gettime(CLOCK_MONOTONIC, &ts);
  return static_cast<uint64_t>(ts.tv_sec) * 1'000'000'000ull +
         static_cast<uint64_t>(ts.tv_nsec) + ms * 1'000'000ull;
}

// xorshift64* — deterministic payload filler, not cryptographic randomness.
uint64_t xs64(uint64_t& st) {
  st ^= st >> 12;
  st ^= st << 25;
  st ^= st >> 27;
  return st * 0x2545F4914F6CDD1Dull;
}

void fill_body(uint8_t* p, uint32_t n, uint64_t seq) {
  uint64_t st = seq * 0x9E3779B97F4A7C15ull + 1;
  for (uint32_t i = 0; i < n; ++i)
    p[i] = static_cast<uint8_t>(xs64(st) >> 32);
}

bool check_body(const uint8_t* p, uint32_t n, uint64_t seq) {
  std::vector<uint8_t> tmp(n);
  fill_body(tmp.data(), n, seq);
  return std::memcmp(tmp.data(), p, n) == 0;
}

const char* gate_str(ShmDesc& d) {
  bool p = shm_gate_held(d, Gate::Producer);
  bool c = shm_gate_held(d, Gate::Consumer);
  if (p && c) return "producer+consumer";
  if (p) return "producer";
  if (c) return "consumer";
  return "-";
}

// ----------------------------------------------------------------- commands

int cmd_create(const Args& a) {
  Config cfg{a.capacity, a.max_payload};
  if (!Ring::valid_config(cfg)) {
    std::fprintf(stderr, "create: bad capacity (power-of-two 2..65536) or max_payload\n");
    return EXIT_USAGE;
  }
  size_t bytes = Ring::bytes_for(cfg);
  ShmDesc d;
  Err e = shm_create(a.name, bytes, d);
  if (e != Err::Ok) {
    std::fprintf(stderr, "create %s: %s\n", a.name, err_string(e));
    return (e == Err::Incompatible) ? EXIT_GATE_LOCKED : 1;
  }
  e = Ring::create(d.base, d.bytes, cfg);
  if (e != Err::Ok) {
    std::fprintf(stderr, "create: %s\n", err_string(e));
    shm_unlink_name(a.name);
    return 1;
  }
  std::printf("created %s capacity=%u max_payload=%u bytes=%zu\n",
              a.name, cfg.capacity, cfg.max_payload, bytes);
  return 0;
}

int cmd_unlink(const Args& a) {
  Err e = shm_unlink_name(a.name);
  if (e != Err::Ok) {
    std::fprintf(stderr, "unlink %s: %s\n", a.name, err_string(e));
    return 1;
  }
  std::printf("removed %s\n", a.name);
  return 0;
}

int cmd_info(const Args& a) {
  ShmDesc d;
  Err e = shm_open_existing(a.name, d);
  if (e != Err::Ok) {
    std::fprintf(stderr, "info %s: %s\n", a.name, err_string(e));
    return 1;
  }
  // Observer: read-only inspection, never runs crash recovery (running
  // recovery from `info` would erase the very WRITING evidence the
  // operator is trying to observe).
  Ring ring(d.base, d.bytes, Role::Observer);
  RecoverReport rep;
  e = ring.open(&rep);
  auto* h = static_cast<Header*>(d.base);
  if (e != Err::Ok) {
    std::fprintf(stderr, "info: %s\n", err_string(e));
    return 1;
  }
  Stats st = ring.stats();
  std::printf("name            %s\n", a.name);
  std::printf("capacity        %u\n", h->capacity);
  std::printf("max_payload     %u\n", h->max_payload);
  std::printf("head            %llu\n", (unsigned long long)st.head);
  std::printf("tail            %llu\n", (unsigned long long)st.tail);
  std::printf("depth           %llu\n", (unsigned long long)st.depth);
  std::printf("wraps_head      %llu\n", (unsigned long long)st.wraps_head);
  std::printf("wraps_tail      %llu\n", (unsigned long long)st.wraps_tail);
  std::printf("publish_total   %llu\n", (unsigned long long)st.publish_total);
  std::printf("consume_total   %llu\n", (unsigned long long)st.consume_total);
  std::printf("publish_timeouts %llu\n", (unsigned long long)st.publish_timeouts);
  std::printf("recovered_slots %llu\n", (unsigned long long)st.recovered_slots);
  std::printf("crc_errors      %llu\n", (unsigned long long)st.crc_errors);
  std::printf("pending_write   %llu\n", (unsigned long long)st.pending_write);
  std::printf("gate            %s\n", gate_str(d));
  return 0;
}

int cmd_producer(const Args& args) {
  Args a = args;
  ShmDesc d;
  Err e = shm_open_existing(a.name, d);
  if (e != Err::Ok) {
    std::fprintf(stderr, "producer: %s\n", err_string(e));
    return 1;
  }
  e = shm_gate_lock(d, Gate::Producer);
  if (e != Err::Ok) {
    std::fprintf(stderr, "producer gate: %s\n", err_string(e));
    return EXIT_GATE_LOCKED;
  }
  Ring ring(d.base, d.bytes, Role::Producer);
  RecoverReport rep;
  e = ring.open(&rep);
  if (e != Err::Ok) {
    std::fprintf(stderr, "producer open: %s\n", err_string(e));
    return (e == Err::ReinitRequired) ? EXIT_REINIT : 1;
  }
  if (rep.ran) {
    std::fprintf(stderr,
                 "recovery: head %llu->%llu adopted=%llu in_flight=%llu\n",
                 (unsigned long long)rep.head_loaded,
                 (unsigned long long)rep.head_committed,
                 (unsigned long long)rep.adopted,
                 (unsigned long long)rep.in_flight);
  }
  // The segment is authoritative for its own record bound; ignore a
  // stale -m passed on the attach side.
  a.max_payload = static_cast<Header*>(d.base)->max_payload;

  // Frame = 20-digit seq + space + body.  fgets needs room for the NUL,
  // and the body buffer must leave the 21-byte prefix below max_payload.
  const uint32_t kPrefix = 21;
  std::vector<uint8_t> buf(a.max_payload);
  std::vector<uint8_t> line(a.max_payload + 1); // fgets NUL terminator
  long long sent = 0;
  // Frame sequence is the persistent ring POSITION, not a per-process
  // counter: a restarted producer resumes at head (e.g. 500) and frames
  // 500,501,... so the consumer can correlate across the crash boundary.
  long long produced = static_cast<long long>(ring.head_pos());
  uint64_t size_rng = 0xC0FFEE1234ull ^ static_cast<uint64_t>(produced);
  // With an input file the default is "send every line until EOF"; for
  // synthetic traffic it is a single message unless -n says otherwise.
  long long limit = a.in_file ? -1 : a.count;
  long long skipped_oversize = 0;

  FILE* inf = nullptr;
  if (a.in_file) {
    inf = std::fopen(a.in_file, "rb");
    if (!inf) {
      std::fprintf(stderr, "producer: cannot open %s\n", a.in_file);
      return 1;
    }
  }

  while (!g_stop && (limit < 0 || sent < limit)) {
    uint32_t body = 0;
    if (inf) {
      if (std::fgets(reinterpret_cast<char*>(line.data()),
                     static_cast<int>(line.size()), inf) == nullptr)
        break; // EOF / read error: all input lines have been offered
      size_t n = std::strlen(reinterpret_cast<char*>(line.data()));
      while (n && (line[n - 1] == '\n' || line[n - 1] == '\r'))
        line[--n] = 0;
      if (n + kPrefix > a.max_payload) {
        std::fprintf(stderr,
                     "producer: input line (%zu bytes) + frame prefix "
                     "exceeds max_payload %u; skipping\n",
                     n, a.max_payload);
        ++skipped_oversize;
        continue;
      }
      std::memcpy(buf.data(), line.data(), n);
      body = static_cast<uint32_t>(n);
    } else {
      uint32_t n = static_cast<uint32_t>(a.size);
      if (a.size_range)
        n = static_cast<uint32_t>(
            a.size + (xs64(size_rng) %
                      static_cast<uint64_t>(a.size_range + 1)));
      if (n > a.max_payload)
        n = a.max_payload;
      fill_body(buf.data(), n, static_cast<uint64_t>(produced));
      body = n;
    }

    Ring::Slot s;
    uint64_t dl = deadline_from_ms(a.timeout_ms);
    e = ring.reserve(s, dl);
    if (e != Err::Ok) {
      if (e == Err::Full) {
        if (!a.quiet)
          std::fprintf(stderr, "producer: full after %llums, sent=%lld\n",
                       (unsigned long long)a.timeout_ms, sent);
        return EXIT_FULL_TIMEOUT;
      }
      std::fprintf(stderr, "producer reserve: %s\n", err_string(e));
      return (e == Err::ReinitRequired) ? EXIT_REINIT : 1;
    }

    // Frame: 20-digit zero-padded sequence, space, body bytes.
    char pfx[24];
    int pn = std::snprintf(pfx, sizeof(pfx), "%020llu ",
                           static_cast<unsigned long long>(produced));
    uint32_t total = static_cast<uint32_t>(pn) + body;
    if (total > a.max_payload) {
      ring.abort_slot(s);
      std::fprintf(stderr, "producer: frame exceeds max_payload\n");
      return 1;
    }    std::memcpy(s.payload, pfx, static_cast<size_t>(pn));
    if (body)
      std::memcpy(s.payload + pn, buf.data(), body);
    uint32_t crc = Ring::crc_record(s.pos, s.payload, total);

    // Fault injection: sit in the WRITING state, then die or commit late.
    if (a.fault_at >= 0 && static_cast<long long>(produced) == a.fault_at) {
      uint64_t end = 0;
      timespec ts0{};
      clock_gettime(CLOCK_MONOTONIC, &ts0);
      end = static_cast<uint64_t>(ts0.tv_sec) * 1'000'000'000ull +
            static_cast<uint64_t>(ts0.tv_nsec) + a.fault_ms * 1'000'000ull;
      while (!g_stop) {
        timespec now{};
        clock_gettime(CLOCK_MONOTONIC, &now);
        uint64_t n = static_cast<uint64_t>(now.tv_sec) * 1'000'000'000ull +
                     static_cast<uint64_t>(now.tv_nsec);
        if (n >= end) break;
        struct timespec sl{0, 50 * 1000 * 1000};
        nanosleep(&sl, nullptr);
      }
      if (a.fault_exit || g_stop)
        _exit(134); // hard exit: slot left WRITING, gate lock auto-released
    }

    e = ring.commit(s, total, crc);
    if (e != Err::Ok) {
      std::fprintf(stderr, "producer commit: %s\n", err_string(e));
      return (e == Err::ReinitRequired) ? EXIT_REINIT : 1;
    }
    ++sent;
    ++produced;
    if (a.pace_us) {
      std::this_thread::sleep_for(
          std::chrono::microseconds(static_cast<long long>(a.pace_us)));
    }
  }
  if (inf)
    std::fclose(inf);
  if (!a.quiet)
    std::fprintf(stderr, "producer: sent=%lld head=%llu\n", sent,
                 (unsigned long long)ring.head_pos());
  return 0;
}

int cmd_consumer(const Args& args) {
  Args a = args;
  ShmDesc d;
  Err e = shm_open_existing(a.name, d);
  if (e != Err::Ok) {
    std::fprintf(stderr, "consumer: %s\n", err_string(e));
    return 1;
  }
  e = shm_gate_lock(d, Gate::Consumer);
  if (e != Err::Ok) {
    std::fprintf(stderr, "consumer gate: %s\n", err_string(e));
    return EXIT_GATE_LOCKED;
  }
  Ring ring(d.base, d.bytes, Role::Consumer);
  e = ring.open();
  if (e != Err::Ok) {
    std::fprintf(stderr, "consumer open: %s\n", err_string(e));
    return (e == Err::ReinitRequired) ? EXIT_REINIT : 1;
  }
  a.max_payload = static_cast<Header*>(d.base)->max_payload;

  long long got = 0;
  bool have_baseline = false;
  uint64_t expected = 0;
  long long duplicates = 0;
  uint64_t last_pos = UINT64_MAX;
  std::vector<uint8_t> buf(a.max_payload);

  while (!g_stop && (a.count < 0 || got < a.count)) {
    Ring::Message m;
    e = ring.consume(m, deadline_from_ms(a.timeout_ms));
    if (e == Err::Empty) {
      if (!a.quiet)
        std::fprintf(stderr, "consumer: idle timeout, got=%lld\n", got);
      return (got == 0) ? EXIT_EMPTY_TIMEOUT : 0;
    }
    if (e != Err::Ok) {
      std::fprintf(stderr, "consumer: %s\n", err_string(e));
      return (e == Err::ReinitRequired) ? EXIT_REINIT : 1;
    }

    e = ring.verify_message(m);
    if (e != Err::Ok) {
      std::fprintf(stderr, "consumer: CRC mismatch at pos=%llu (torn/corrupt)\n",
                   (unsigned long long)m.pos);
      return EXIT_INTEGRITY;
    }
    if (m.len > buf.size()) {
      std::fprintf(stderr, "consumer: frame too large\n");
      return EXIT_INTEGRITY;
    }
    std::memcpy(buf.data(), m.payload, m.len);

    uint64_t pos = 0;
    bool framed = false;
    if (m.len >= 21 && buf[20] == ' ') {
      char tmp[21] = {};
      std::memcpy(tmp, buf.data(), 20);
      char* end = nullptr;
      unsigned long long v = strtoull(tmp, &end, 10);
      if (end == tmp + 20) {
        pos = v;
        framed = true;
      }
    }

    if (last_pos != UINT64_MAX && m.pos <= last_pos) {
      // Redelivery after an earlier consumer crash is allowed (at-least-once)
      // but must never go backwards beyond one ring cycle.
      if (m.pos + static_cast<uint64_t>(
                       static_cast<Header*>(d.base)->capacity) <= last_pos) {
        std::fprintf(stderr, "consumer: non-monotonic position %llu after %llu\n",
                     (unsigned long long)m.pos, (unsigned long long)last_pos);
        return EXIT_INTEGRITY;
      }
      ++duplicates;
    }
    last_pos = m.pos;

    if (framed && a.verify_content) {
      if (pos != m.pos) {
        std::fprintf(stderr,
                     "consumer: frame seq mismatch frame=%llu slot=%llu\n",
                     (unsigned long long)pos,
                     (unsigned long long)m.pos);
        return EXIT_INTEGRITY;
      }
      if (!check_body(buf.data() + 21, m.len - 21, m.pos)) {
        std::fprintf(stderr, "consumer: payload corruption at pos=%llu\n",
                     (unsigned long long)m.pos);
        return EXIT_INTEGRITY;
      }
      // A restarted consumer legitimately resumes mid-stream: establish the
      // expectation on its first delivered position instead of demanding 0.
      if (!have_baseline) {
        expected = m.pos;
        have_baseline = true;
      }
      if (duplicates == 0 && m.pos != expected) {
        std::fprintf(stderr, "consumer: gap expected=%llu got=%llu\n",
                     (unsigned long long)expected,
                     (unsigned long long)m.pos);
        return EXIT_INTEGRITY;
      }
      if (m.pos >= expected)
        expected = m.pos + 1;
    }

    // Emit exactly one physical line per record: escape '\n' and '\\' so
    // that binary payloads (which may contain any byte) do not break
    // line-based accounting. The record boundaries themselves are the
    // ring slots, not the newlines.
    for (uint32_t k = 0; k < m.len; ++k) {
      uint8_t ch = buf[k];
      if (ch == '\\')
        std::fputs("\\\\", stdout);
      else if (ch == '\n')
        std::fputs("\\n", stdout);
      else
        std::fputc(static_cast<int>(ch), stdout);
    }
    std::fputc('\n', stdout);
    std::fflush(stdout);

    e = ring.release(m);
    if (e != Err::Ok) {
      std::fprintf(stderr, "consumer release: %s\n", err_string(e));
      return (e == Err::ReinitRequired) ? EXIT_REINIT : 1;
    }
    ++got;
    if (a.pace_us)
      std::this_thread::sleep_for(
          std::chrono::microseconds(static_cast<long long>(a.pace_us)));
  }
  if (!a.quiet)
    std::fprintf(stderr,
                 "consumer: got=%lld tail=%llu duplicates=%lld\n", got,
                 (unsigned long long)ring.tail_pos(),
                 (long long)duplicates);
  // Exit code carries the duplicate count for the fixtures (0..200).
  return static_cast<int>(std::min<long long>(200, duplicates));
}

int cmd_debug_corrupt(const Args& a) {
  ShmDesc d;
  Err e = shm_open_existing(a.name, d);
  if (e != Err::Ok)
    return 1;
  Ring ring(d.base, d.bytes, Role::Either);
  e = ring.open();
  if (e != Err::Ok)
    return (e == Err::ReinitRequired) ? EXIT_REINIT : 1;
  e = ring.debug_flip_payload(static_cast<uint64_t>(a.fault_at < 0 ? 0 : a.fault_at));
  if (e != Err::Ok) {
    std::fprintf(stderr, "debug-corrupt: %s\n", err_string(e));
    return 1;
  }
  std::printf("flipped payload byte at pos=%lld\n", a.fault_at);
  return 0;
}

// -------------------------------------------------------------------- usage

void usage() {
  std::fprintf(stderr,
      "usage: shmrq <command> /name [options]\n"
      "  create    [-c CAPACITY] [-m MAX_PAYLOAD]\n"
      "  unlink\n"
      "  info\n"
      "  producer  [-n COUNT|-1] [-s SIZE] [-r SIZE_RANGE] [-f FILE]\n"
      "            [-t FULL_TIMEOUT_MS] [--pace-us US]\n"
      "            [--fault-at SEQ --fault-ms MS] [--fault-exit]\n"
      "  consumer  [-n COUNT|-1] [-t IDLE_TIMEOUT_MS] [--pace-us US]\n"
      "            [--no-verify]\n"
      "  debug-corrupt --fault-at POS\n"
      "exit codes: 0 ok 2 gate-locked 3 integrity 4 empty-timeout\n"
      "            5 full-timeout 6 usage 7 reinit-required\n");
}

} // namespace

int main(int argc, char** argv) {
  signal(SIGINT, on_signal);
  signal(SIGTERM, on_signal);

  if (argc < 3) {
    usage();
    return EXIT_USAGE;
  }
  std::string cmd = argv[1];
  Args a;
  a.name = argv[2];

  for (int i = 3; i < argc; ++i) {
    std::string o = argv[i];
    auto next = [&](const char* what) -> const char* {
      if (i + 1 >= argc) {
        std::fprintf(stderr, "missing value for %s\n", what);
        std::exit(EXIT_USAGE);
      }
      return argv[++i];
    };
    if (o == "-c" || o == "--capacity") {
      if (!parse_u32(next(o.c_str()), a.capacity)) return EXIT_USAGE;
    } else if (o == "-m" || o == "--max-payload") {
      if (!parse_u32(next(o.c_str()), a.max_payload)) return EXIT_USAGE;
    } else if (o == "-n" || o == "--count") {
      if (!parse_ll(next(o.c_str()), a.count)) return EXIT_USAGE;
    } else if (o == "-s" || o == "--size") {
      a.size = std::atoi(next(o.c_str()));
    } else if (o == "-r" || o == "--size-range") {
      a.size_range = std::atoi(next(o.c_str()));
    } else if (o == "-f" || o == "--file") {
      a.in_file = next(o.c_str());
    } else if (o == "-t" || o == "--timeout-ms") {
      if (!parse_u64(next(o.c_str()), a.timeout_ms)) return EXIT_USAGE;
    } else if (o == "--pace-us") {
      if (!parse_u64(next(o.c_str()), a.pace_us)) return EXIT_USAGE;
    } else if (o == "--fault-at") {
      if (!parse_ll(next(o.c_str()), a.fault_at)) return EXIT_USAGE;
    } else if (o == "--fault-ms") {
      if (!parse_u64(next(o.c_str()), a.fault_ms)) return EXIT_USAGE;
    } else if (o == "--fault-exit") {
      a.fault_exit = true;
    } else if (o == "--no-verify") {
      a.verify_content = false;
    } else if (o == "-q" || o == "--quiet") {
      a.quiet = true;
    } else {
      std::fprintf(stderr, "unknown option: %s\n", o.c_str());
      return EXIT_USAGE;
    }
  }

  if (cmd == "create") return cmd_create(a);
  if (cmd == "unlink" || cmd == "reset") return cmd_unlink(a);
  if (cmd == "info") return cmd_info(a);
  if (cmd == "producer") return cmd_producer(a);
  if (cmd == "consumer") return cmd_consumer(a);
  if (cmd == "debug-corrupt") return cmd_debug_corrupt(a);
  usage();
  return EXIT_USAGE;
}
