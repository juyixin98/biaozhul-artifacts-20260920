// SPDX-License-Identifier: MIT
// Shared-memory SPSC ring queue — on-wire ABI between processes.
// Nothing in this file may hold raw pointers: every cross-process
// reference is an offset from the mapping base.
#pragma once

#include <atomic>
#include <cstddef>
#include <cstdint>

namespace shmrq {

inline constexpr uint32_t kMagic = 0x53484D52; // "SHMR"
inline constexpr uint32_t kVersion = 1;

// Fixed, documented bounds.  Capacity must be a power of two in
// [2, 65536]; a single record payload is 0..max_payload bytes
// (a 0-byte record is legal and useful as a heartbeat).
inline constexpr uint32_t kMinCapacity = 2;
inline constexpr uint32_t kMaxCapacity = 65536;
inline constexpr uint32_t kDefaultCapacity = 64;
inline constexpr uint32_t kDefaultMaxPayload = 4000;
inline constexpr uint32_t kMaxPayloadHard = (1u << 24) - 1; // len is 24 bits
inline constexpr size_t kHeaderBytes = 320;
inline constexpr size_t kSlotAlign = 64;

enum class Role { Producer, Consumer, Either, Observer };

enum class Err {
  Ok = 0,
  Empty,          // no committed slot right now (non-blocking consume)
  Full,           // queue full and wait expired / non-blocking publish
  TooLarge,       // payload exceeds max_payload
  NoMem,          // mapping / allocation failure
  InvalidName,    // shm name must look like /name
  Incompatible,   // magic/version mismatch
  BadParam,
  Locked,         // another producer/consumer (alive process) owns the gate
  ReinitRequired, // structural damage / torn data beyond the defined recovery
  TimeoutSys,     // clock or futex failure
};

const char* err_string(Err e) noexcept;

// Plain CRC-32/ISO-HDLC (IEEE 802.3), init=0xFFFFFFFF, xorout=0xFFFFFFFF.
// Known answer: crc32_ieee("123456789") == 0xCBF43926.
uint32_t crc32_ieee(const void* data, size_t len) noexcept;

struct Config {
  uint32_t capacity = kDefaultCapacity;
  uint32_t max_payload = kDefaultMaxPayload;
};

struct RecoverReport {
  bool ran = false;           // recovery path executed
  uint64_t head_loaded = 0;   // producer head hint found in the header
  uint64_t head_committed = 0;// head advanced to the last fully committed slot
  uint64_t adopted = 0;       // stale slots reclaimed (torn writes)
  uint64_t in_flight = 0;     // committed but unconsumed messages preserved
};

struct Stats {
  uint64_t head = 0;
  uint64_t tail = 0;
  uint64_t depth = 0;
  uint64_t wraps_head = 0;
  uint64_t wraps_tail = 0;
  uint64_t recovered_slots = 0;
  uint64_t crc_errors = 0;
  uint64_t publish_total = 0;
  uint64_t consume_total = 0;
  uint64_t publish_timeouts = 0;
  uint64_t pending_write = 0; // slots currently tagged WRITING (crash evidence)
};

// ---------------------------------------------------------------- ABI types

// One record's integrity tag.  CRC32 (IEEE 802.3) over
//   little-endian u64 seq || little-endian u32 len || payload bytes.
// It is an accidental-bit-rot / torn-write detector, not a cryptographic
// authentication tag; the README says so explicitly.
struct SlotHdr {
  // Fixed 32-byte layout; all metadata fields are atomic, so a reader in
  // another process never observes a torn word. The `seq` word is the
  // single publication point (release store by producer, acquire load by
  // consumer); len/crc/tag are ordered under that release.
  std::atomic<uint64_t> seq;      // Vyukov commit/ack marker
  std::atomic<uint32_t> tag;      // 0 = clean, kWriting = producer active
  std::atomic<uint32_t> len;      // payload bytes, published at commit
  std::atomic<uint32_t> crc;      // record CRC, published at commit
  uint32_t reserved;              // always zero
};

static_assert(sizeof(SlotHdr) == 24, "SlotHdr layout drifted");

inline constexpr uint32_t kWriting = 0x57524954; // "WRIT"

// Header layout.  head/tail are monotonic 64-bit *positions* (number of
// records ever published / consumed), so ring-index wrap and even
// position wrap are observable; they are cache-line separated.
struct Header {
  uint32_t magic;
  uint32_t version;
  uint32_t capacity;
  uint32_t max_payload;
  uint64_t created_boot_id;   // best-effort host/boot tag
  uint64_t total_bytes;
  uint64_t pad_cfg[2];

  alignas(64) std::atomic<uint64_t> head; // producer: committed positions
  std::atomic<uint64_t> publish_total;
  std::atomic<uint64_t> publish_timeouts;
  std::atomic<uint64_t> wraps_head;
  std::atomic<uint64_t> recovered_slots;
  uint64_t pad_h[3];

  alignas(64) std::atomic<uint64_t> tail; // consumer: consumed positions
  std::atomic<uint64_t> consume_total;
  std::atomic<uint64_t> wraps_tail;
  std::atomic<uint64_t> crc_errors;
  uint64_t pad_t[4];

  // Futex words for blocking.  Monotonic counters; wrap of a 32-bit word
  // is harmless for SPSC futex wake semantics.
  alignas(64) std::atomic<int> producer_wake;
  std::atomic<int> producer_generation; // bumped on every publish timeout
  alignas(64) std::atomic<int> consumer_wake;
  std::atomic<int> consumer_generation;

  uint8_t reserved[96];
};

// Queue over an externally provided mapping (POSIX shm or MAP_ANONYMOUS
// for in-process tests).  One Ring per role; blocking takes an optional
// CLOCK_MONOTONIC deadline in nanoseconds (0 = no wait / one poll).
class Ring {
 public:
  // Validation helpers
  static bool valid_config(const Config& c) noexcept;
  static size_t bytes_for(const Config& c) noexcept;

  // Create/attach to a mapping.  For Producer, open() runs the crash
  // recovery protocol and fills *report; Consumer attaches read-side.
  static Err create(void* base, size_t bytes, const Config& cfg) noexcept;
  Ring(void* base, size_t bytes, Role role);
  Err open(RecoverReport* report = nullptr) noexcept;

  // Producer: two-phase publish (reserve -> copy -> commit).  The gap
  // between reserve and commit is exactly where a killed producer leaves
  // evidence: the slot tag is WRITING and its seq is not advanced.
  struct Slot {
    uint32_t index;
    uint64_t pos;
    uint8_t* payload; // write here, max_payload bytes
  };
  Err reserve(Slot& out, uint64_t deadline_ns) noexcept;
  // crc is computed by the caller via crc_record(); commit just stores it.
  Err commit(const Slot& s, uint32_t len, uint32_t crc) noexcept;
  void abort_slot(const Slot& s) noexcept; // clear tag, give slot back

  // Consumer
  struct Message {
    uint32_t index;
    uint64_t pos;
    uint32_t len;
    uint32_t crc;
    const uint8_t* payload;
  };
  Err consume(Message& out, uint64_t deadline_ns) noexcept;
  // Verify CRC in place (without releasing the slot).
  Err verify_message(const Message& m) const noexcept;
  Err release(const Message& m) noexcept;

  // Convenience single call for callers that already have the bytes.
  Err publish(const void* data, uint32_t len, uint64_t deadline_ns) noexcept;

  Stats stats() const noexcept;

  // Test/operator tooling
  // Corrupt one payload byte of the slot that currently carries position.
  Err debug_flip_payload(uint64_t pos) noexcept;
  // Validate a raw record buffer (for unit tests).
  static uint32_t crc_record(uint64_t seq, const void* data, uint32_t len) noexcept;

  uint64_t head_pos() const noexcept { return h_->head.load(std::memory_order_relaxed); }
  uint64_t tail_pos() const noexcept { return h_->tail.load(std::memory_order_relaxed); }

 private:
  SlotHdr* slot(uint32_t i) const noexcept;
  uint8_t* payload(SlotHdr* s) const noexcept;
  Err run_recovery(RecoverReport* report) noexcept;
  Err check_slot_record(const SlotHdr* sh, uint64_t seq) const noexcept;

  Header* h_;
  uint8_t* base_;
  size_t bytes_;
  Role role_;
  uint32_t mask_ = 0;
  uint32_t slot_bytes_ = 0;
  uint64_t local_head_ = 0; // producer cached position
  uint64_t local_tail_ = 0; // consumer cached / producer recovered tail
};

} // namespace shmrq
