// SPDX-License-Identifier: MIT
#include "shmrq/ring.h"

#include <atomic>
#include <cstring>
#include <ctime>

#include "shmrq/futex.h"

namespace shmrq {

const char* err_string(Err e) noexcept {
  switch (e) {
  case Err::Ok: return "ok";
  case Err::Empty: return "empty";
  case Err::Full: return "full";
  case Err::TooLarge: return "payload larger than max_payload";
  case Err::NoMem: return "memory mapping failure";
  case Err::InvalidName: return "invalid shm name";
  case Err::Incompatible: return "magic/version mismatch";
  case Err::BadParam: return "bad parameter";
  case Err::Locked: return "gate held by a live process";
  case Err::ReinitRequired: return "irrecoverable state: explicit reinit required";
  case Err::TimeoutSys: return "clock/futex system failure";
  }
  return "unknown";
}

static uint64_t now_ns() noexcept {
  timespec ts{};
  clock_gettime(CLOCK_MONOTONIC, &ts);
  return static_cast<uint64_t>(ts.tv_sec) * 1'000'000'000ull +
         static_cast<uint64_t>(ts.tv_nsec);
}

static size_t align_up(size_t n, size_t a) noexcept { return (n + a - 1) & ~(a - 1); }

bool Ring::valid_config(const Config& c) noexcept {
  if (c.capacity < kMinCapacity || c.capacity > kMaxCapacity)
    return false;
  if ((c.capacity & (c.capacity - 1)) != 0) // power of two
    return false;
  if (c.max_payload > kMaxPayloadHard)
    return false;
  return true;
}

size_t Ring::bytes_for(const Config& c) noexcept {
  size_t slot = align_up(sizeof(SlotHdr) + c.max_payload, kSlotAlign);
  return kHeaderBytes + static_cast<size_t>(c.capacity) * slot;
}

Err Ring::create(void* base, size_t bytes, const Config& cfg) noexcept {
  if (!base || !valid_config(cfg))
    return Err::BadParam;
  if (bytes < bytes_for(cfg))
    return Err::NoMem;
  if (!std::atomic<uint64_t>::is_always_lock_free ||
      !std::atomic<uint32_t>::is_always_lock_free ||
      !std::atomic<int>::is_always_lock_free) {
    // Process-shared atomics must be genuinely lock-free; a mutex inside
    // a shared mapping would use process-private futex state.
    return Err::NoMem;
  }
  std::memset(base, 0, bytes);
  auto* h = new (base) Header;
  h->magic = kMagic;
  h->version = kVersion;
  h->capacity = cfg.capacity;
  h->max_payload = cfg.max_payload;
  h->total_bytes = bytes;

  h->head.store(0, std::memory_order_relaxed);
  h->tail.store(0, std::memory_order_relaxed);
  h->producer_wake.store(0, std::memory_order_relaxed);
  h->consumer_wake.store(0, std::memory_order_relaxed);

  auto* hb = static_cast<uint8_t*>(base);
  for (uint32_t i = 0; i < cfg.capacity; ++i) {
    auto* s = new (hb + kHeaderBytes +
                   static_cast<size_t>(i) *
                       align_up(sizeof(SlotHdr) + cfg.max_payload, kSlotAlign))
        SlotHdr;
    // Empty round: slot i is waiting for producer position i.
    s->seq.store(i, std::memory_order_relaxed);
    s->tag.store(0, std::memory_order_relaxed);
    s->len.store(0, std::memory_order_relaxed);
    s->crc.store(0, std::memory_order_relaxed);
    s->reserved = 0;
  }
  return Err::Ok;
}

Ring::Ring(void* base, size_t bytes, Role role)
    : base_(static_cast<uint8_t*>(base)), bytes_(bytes), role_(role) {}

Err Ring::open(RecoverReport* report) noexcept {
  if (!base_)
    return Err::BadParam;
  h_ = reinterpret_cast<Header*>(base_);
  if (h_->magic != kMagic || h_->version != kVersion)
    return Err::Incompatible;
  Config cfg{h_->capacity, h_->max_payload};
  if (!valid_config(cfg) || h_->total_bytes > bytes_ ||
      bytes_for(cfg) > bytes_)
    return Err::Incompatible;
  mask_ = cfg.capacity - 1;
  slot_bytes_ =
      static_cast<uint32_t>(align_up(sizeof(SlotHdr) + cfg.max_payload, kSlotAlign));

  local_tail_ = h_->tail.load(std::memory_order_acquire);

  if (role_ == Role::Consumer || role_ == Role::Either) {
    // Slot-driven restart recovery (mirrors run_recovery but never
    // mutates, and resumes at the first still-committed record):
    //   seq == p+cap / p+1+cap -> freed/reused: record is history
    //   seq == p+1             -> live record: deliver it (at-least-once
    //                             for anything unacked before the crash)
    //   seq == p               -> free frontier: nothing pending
    uint64_t p = local_tail_;
    for (uint32_t steps = 0; steps <= h_->capacity; ++steps) {
      SlotHdr* s = slot(static_cast<uint32_t>(p & mask_));
      uint64_t cur = s->seq.load(std::memory_order_acquire);
      if (cur == p + h_->capacity || cur == p + 1 + h_->capacity) {
        ++p;
        continue;
      }
      break; // seq == p (empty) or seq == p+1 (live) or corrupt
    }
    local_tail_ = p;
  }

  if (role_ == Role::Producer || role_ == Role::Either) {
    Err e = run_recovery(report);
    if (e != Err::Ok)
      return e;
    // run_recovery adopted any lag-free tail into local_tail_; keep it.
  }
  return Err::Ok;
}

SlotHdr* Ring::slot(uint32_t i) const noexcept {
  return reinterpret_cast<SlotHdr*>(base_ + kHeaderBytes +
                                    static_cast<size_t>(i) * slot_bytes_);
}

uint8_t* Ring::payload(SlotHdr* s) const noexcept {
  return reinterpret_cast<uint8_t*>(s) + sizeof(SlotHdr);
}

// ----------------------------------------------------------------- recovery
//
// Invariants (positions are monotonic uint64; index = pos & mask;
// slot i initial seq = i, Vyukov layout):
//   slot for position p, seq value:
//     == p            -> free for this round (empty / reclaimable torn)
//     == p + 1        -> committed this round, awaiting consumer
//     == p + 1 - cap  -> still committed from the previous round (queue full)
//     anything else   -> structural corruption
//
// The head/tail header words are *hints*: the authoritative state is the
// per-slot sequence.  A producer crash leaves exactly one of two states at
// the candidate slot head:
//   tag == WRITING, seq == head  -> died mid-record: slot never committed,
//                                   safe to reclaim (unconfirmed data).
//   tag == 0,       seq == head  -> died before/without reserving, or right
//                                   after clearing tag: nothing committed.
// A committed slot (seq == head+1) is always preserved.

Err Ring::check_slot_record(const SlotHdr* sh, uint64_t p) const noexcept {
  uint32_t len = sh->len.load(std::memory_order_acquire);
  if (len > h_->max_payload)
    return Err::ReinitRequired;
  uint32_t want =
      crc_record(p, payload(const_cast<SlotHdr*>(sh)), len);
  if (want != sh->crc.load(std::memory_order_acquire)) {
    h_->crc_errors.fetch_add(1, std::memory_order_relaxed);
    return Err::ReinitRequired;
  }
  return Err::Ok;
}

Err Ring::run_recovery(RecoverReport* report) noexcept {
  RecoverReport r{};
  r.ran = true;
  uint64_t hint_head = h_->head.load(std::memory_order_acquire);
  uint64_t hint_tail = h_->tail.load(std::memory_order_acquire);
  r.head_loaded = hint_head;

  const uint64_t cap = h_->capacity;

  // Slot seq encodes position AND state unambiguously (initial seq=i;
  // committed at p -> p+1; released -> p+cap; reused at p+cap -> p+cap+1).
  // The header words are only hints that may lag, so recovery trusts the
  // slots and walks:
  //
  // 1) Skip a freed/reused prefix.  These positions are history: either
  //    the consumer released them and died before its tail ledger, or the
  //    producer has since wrapped the slot.  A live record (seq == p+1)
  //    or a free slot (seq == p) ends the walk.
  uint64_t tail = hint_tail;
  for (uint32_t steps = 0; steps <= cap; ++steps) {
    uint64_t cur = slot(static_cast<uint32_t>(tail & mask_))
                       ->seq.load(std::memory_order_acquire);
    if (cur == tail + cap || cur == tail + 1 + cap) {
      ++tail;
      continue;
    }
    break;
  }

  // 2) Walk the contiguous committed range [tail, head), CRC-checking
  //    every record.  Commits are ordered, so this run is contiguous; it
  //    ends at the free frontier (seq == head) or at a full queue where
  //    the frontier slot still carries the previous round's record.
  uint64_t head = tail;
  for (uint32_t steps = 0; steps <= cap; ++steps) {
    SlotHdr* s = slot(static_cast<uint32_t>(head & mask_));
    uint64_t cur = s->seq.load(std::memory_order_acquire);
    if (cur == head + 1) {
      Err e = check_slot_record(s, head);
      if (e != Err::Ok)
        return e;
      ++head;
      continue;
    }
    break; // free frontier, torn-write slot, or full-queue slot
  }

  if (head < tail || head - tail > cap)
    return Err::ReinitRequired;

  // 3) Validate the classification of every slot in the window and reclaim
  //    exactly one uncommitted (torn) write at the frontier.
  for (uint64_t p = tail; p < head; ++p) {
    SlotHdr* s = slot(static_cast<uint32_t>(p & mask_));
    if (s->seq.load(std::memory_order_acquire) != p + 1)
      return Err::ReinitRequired; // gap in the committed range
  }
  if (head - tail < cap) {
    SlotHdr* s = slot(static_cast<uint32_t>(head & mask_));
    uint64_t cur = s->seq.load(std::memory_order_acquire);
    if (cur != head) {
      // Expected the free value.  Anything else here means the committed
      // walk stopped for a reason that is not a reclaimable slot.
      return Err::ReinitRequired;
    }
    if (s->tag.load(std::memory_order_acquire) == kWriting) {
      s->tag.store(0, std::memory_order_relaxed);
      r.adopted = 1;
      h_->recovered_slots.fetch_add(1, std::memory_order_relaxed);
    }
  } else {
    // Full: the frontier slot must still show the prior committed round.
    SlotHdr* s = slot(static_cast<uint32_t>(head & mask_));
    if (s->seq.load(std::memory_order_acquire) != head + 1 - cap)
      return Err::ReinitRequired;
  }
  // The committed frontier may legitimately be ahead of the head hint
  // (crash between the slot seq store and the relaxed head store); it can
  // never be behind it.
  if (hint_head > head || hint_tail > tail)
    return Err::ReinitRequired;

  h_->head.store(head, std::memory_order_release);
  // NOTE: tail is owned by the consumer role.  A restarted producer may
  // observe it lagging behind slot reality; it adopts the corrected value
  // only LOCALLY for space accounting, never writes the word — a live
  // consumer may be advancing the same word concurrently.
  r.head_committed = head;
  r.in_flight = head - tail;
  local_head_ = head;
  local_tail_ = tail;
  if (report)
    *report = r;
  return Err::Ok;
}

// ---------------------------------------------------------------- producer

Err Ring::reserve(Slot& out, uint64_t deadline_ns) noexcept {
  if (role_ != Role::Producer && role_ != Role::Either)
    return Err::BadParam;
  if (local_head_ == UINT64_MAX)
    return Err::ReinitRequired; // 64-bit position space exhausted

  for (;;) {
    uint32_t idx = static_cast<uint32_t>(local_head_ & mask_);
    SlotHdr* s = slot(idx);
    uint64_t cur = s->seq.load(std::memory_order_acquire);
    int64_t diff = static_cast<int64_t>(cur - local_head_);

    if (diff == 0) {
      // Free slot.  Mark WRITING (relaxed: this is a local-only reservation;
      // a concurrent observer may or may not see it, and MUST NOT act on
      // payload bytes until the commit release publishes the record).
      s->tag.store(kWriting, std::memory_order_relaxed);
      out.index = idx;
      out.pos = local_head_;
      out.payload = payload(s);
      return Err::Ok;
    }
    if (diff == 1 - static_cast<int64_t>(h_->capacity)) {
      // seq == pos + 1 - cap: previous round's record still unconsumed
      // (Vykukov "dif < 0": queue full).
      uint64_t ht = h_->tail.load(std::memory_order_acquire);
      uint64_t t = ht > local_tail_ ? ht : local_tail_; // max(hint, recovered)
      if (t > local_head_)
        return Err::ReinitRequired;
      if (local_head_ - t < h_->capacity)
        continue; // consumer released a slot between checks
      if (deadline_ns == 0) {
        h_->publish_timeouts.fetch_add(1, std::memory_order_relaxed);
        return Err::Full;
      }
      if (now_ns() >= deadline_ns) {
        h_->publish_timeouts.fetch_add(1, std::memory_order_relaxed);
        return Err::Full;
      }
      int observed = h_->producer_wake.load(std::memory_order_relaxed);
      ht = h_->tail.load(std::memory_order_acquire);
      t = ht > local_tail_ ? ht : local_tail_;
      if (t <= local_head_ && local_head_ - t >= h_->capacity) {
        FutexRes fr = futex_wait_bitset(h_->producer_wake, observed, deadline_ns);
        if (fr == FutexRes::Error)
          return Err::TimeoutSys;
        if (fr == FutexRes::Timeout || now_ns() >= deadline_ns) {
          h_->publish_timeouts.fetch_add(1, std::memory_order_relaxed);
          return Err::Full;
        }
      }
      uint64_t adv = h_->tail.load(std::memory_order_acquire);
      if (adv > local_tail_)
        local_tail_ = adv;
      continue;
    }
    return Err::ReinitRequired;
  }
}

Err Ring::commit(const Slot& s, uint32_t len, uint32_t crc) noexcept {
  if (role_ != Role::Producer && role_ != Role::Either)
    return Err::BadParam;
  if (s.pos != local_head_ || s.index != static_cast<uint32_t>(s.pos & mask_))
    return Err::BadParam;
  if (len > h_->max_payload)
    return Err::TooLarge;

  SlotHdr* sh = slot(s.index);
  sh->len.store(len, std::memory_order_relaxed);
  sh->crc.store(crc, std::memory_order_relaxed);
  sh->reserved = 0;
  std::atomic_thread_fence(std::memory_order_release);
  // After the fence: clearing tag first, then committing the sequence.
  // A crash in the gap leaves an uncommitted, untagged slot that recovery
  // reclaims as empty without trusting any bytes; only the seq release
  // makes the record observable to the consumer.
  sh->tag.store(0, std::memory_order_relaxed);
  sh->seq.store(s.pos + 1, std::memory_order_release);

  uint64_t newhead = s.pos + 1;
  if ((newhead & mask_) == 0)
    h_->wraps_head.fetch_add(1, std::memory_order_relaxed);
  h_->head.store(newhead, std::memory_order_relaxed);
  h_->publish_total.fetch_add(1, std::memory_order_relaxed);
  local_head_ = newhead;

  // Wake a blocked consumer: bump (release) then futex_wake; the consumer
  // re-reads head after waking so even a lost signal only costs a timeout.
  h_->consumer_wake.fetch_add(1, std::memory_order_release);
  futex_wake_bitset(h_->consumer_wake, 1);
  return Err::Ok;
}

void Ring::abort_slot(const Slot& s) noexcept {
  SlotHdr* sh = slot(s.index);
  sh->tag.store(0, std::memory_order_relaxed);
}

Err Ring::publish(const void* data, uint32_t len, uint64_t deadline_ns) noexcept {
  if (len > h_->max_payload)
    return Err::TooLarge;
  if (len && !data)
    return Err::BadParam;
  Slot s;
  Err e = reserve(s, deadline_ns);
  if (e != Err::Ok)
    return e;
  if (len)
    std::memcpy(s.payload, data, len);
  uint32_t crc = crc_record(s.pos, data, len);
  return commit(s, len, crc);
}

// ---------------------------------------------------------------- consumer

Err Ring::consume(Message& out, uint64_t deadline_ns) noexcept {
  if (role_ != Role::Consumer && role_ != Role::Either)
    return Err::BadParam;

  for (;;) {
    uint32_t idx = static_cast<uint32_t>(local_tail_ & mask_);
    SlotHdr* s = slot(idx);
    uint64_t cur = s->seq.load(std::memory_order_acquire);
    int64_t diff = static_cast<int64_t>(cur - local_tail_);

    if (diff == 1) {
      // Committed.  Tag must have been cleared before the seq commit;
      // an acquire load pairs with the producer's release tag/seq stores.
      if (s->tag.load(std::memory_order_acquire) == kWriting)
        return Err::ReinitRequired;
      uint32_t len = s->len.load(std::memory_order_acquire);
      if (len > h_->max_payload)
        return Err::ReinitRequired;
      out.index = idx;
      out.pos = local_tail_;
      out.len = len;
      out.crc = s->crc.load(std::memory_order_acquire);
      out.payload = payload(s);
      return Err::Ok; // acquire-load of seq makes payload visible
    }
    if (diff == 0) {
      // Empty (the slot's free value seq == pos is authoritative).  The
      // header head word is only a wake hint and can lag the per-slot
      // frontier, so it is never treated as corruption here.
      if (deadline_ns == 0)
        return Err::Empty;
      if (now_ns() >= deadline_ns)
        return Err::Empty;
      int observed = h_->consumer_wake.load(std::memory_order_relaxed);
      uint64_t hd = h_->head.load(std::memory_order_acquire);
      if (hd <= local_tail_) {
        FutexRes fr = futex_wait_bitset(h_->consumer_wake, observed, deadline_ns);
        if (fr == FutexRes::Error)
          return Err::TimeoutSys;
        if (fr == FutexRes::Timeout || now_ns() >= deadline_ns)
          return Err::Empty;
      }
      continue;
    }
    if (cur == local_tail_ + h_->capacity ||
        cur == local_tail_ + 1 + h_->capacity) {
      // Slot freed/reused while our tail hint lagged (post-crash open
      // window): advance the hint past it.
      ++local_tail_;
      continue;
    }
    return Err::ReinitRequired;
  }
}

Err Ring::verify_message(const Message& m) const noexcept {
  if (m.len > h_->max_payload)
    return Err::ReinitRequired;
  uint32_t want = crc_record(m.pos, m.payload, m.len);
  if (want != m.crc) {
    h_->crc_errors.fetch_add(1, std::memory_order_relaxed);
    return Err::ReinitRequired;
  }
  return Err::Ok;
}

Err Ring::release(const Message& m) noexcept {
  if (role_ != Role::Consumer && role_ != Role::Either)
    return Err::BadParam;
  if (m.pos != local_tail_ || m.index != static_cast<uint32_t>(m.pos & mask_))
    return Err::BadParam;

  SlotHdr* sh = slot(m.index);
  uint64_t newtail = m.pos + 1;

  // Vyukov ownership order: the slot is handed back (seq := p+cap, the
  // exact "free" value the producer waits for next lap) BEFORE the tail
  // ledger moves.  Reversing these two would let the producer reuse a
  // slot the consumer has not yet released, which corrupts ownership.
  //
  // Crash semantics:
  //   - crash before this call  -> record redelivered (at-least-once);
  //   - crash after seq store but before tail store -> slot is free,
  //     tail hint lags.  The restarted consumer re-reads the authoritative
  //     per-slot state via run_recovery-equivalent scan: it sees seq that
  //     is neither p nor p+1 for the old position and advances past slots
  //     already reused, delivering only what still carries seq == pos+1;
  //   - after tail store -> record acknowledged, never redelivered.
  sh->tag.store(0, std::memory_order_relaxed);
  sh->seq.store(m.pos + h_->capacity, std::memory_order_release);

  if ((newtail & mask_) == 0)
    h_->wraps_tail.fetch_add(1, std::memory_order_relaxed);
  h_->tail.store(newtail, std::memory_order_release);
  h_->consume_total.fetch_add(1, std::memory_order_relaxed);
  local_tail_ = newtail;

  h_->producer_wake.fetch_add(1, std::memory_order_release);
  futex_wake_bitset(h_->producer_wake, 1);
  return Err::Ok;
}

// ----------------------------------------------------------------- utility

Stats Ring::stats() const noexcept {
  Stats st{};
  uint64_t hd = h_->head.load(std::memory_order_acquire);
  uint64_t tl = h_->tail.load(std::memory_order_acquire);
  st.head = hd;
  st.tail = tl;
  st.depth = tl <= hd ? hd - tl : 0;
  st.wraps_head = h_->wraps_head.load(std::memory_order_relaxed);
  st.wraps_tail = h_->wraps_tail.load(std::memory_order_relaxed);
  st.recovered_slots = h_->recovered_slots.load(std::memory_order_relaxed);
  st.crc_errors = h_->crc_errors.load(std::memory_order_relaxed);
  st.publish_total = h_->publish_total.load(std::memory_order_relaxed);
  st.consume_total = h_->consume_total.load(std::memory_order_relaxed);
  st.publish_timeouts = h_->publish_timeouts.load(std::memory_order_relaxed);
  for (uint32_t i = 0; i < h_->capacity; ++i) {
    if (slot(i)->tag.load(std::memory_order_acquire) == kWriting)
      ++st.pending_write;
  }
  return st;
}

Err Ring::debug_flip_payload(uint64_t p) noexcept {
  SlotHdr* sh = slot(static_cast<uint32_t>(p & mask_));
  if (sh->seq.load(std::memory_order_acquire) != p + 1)
    return Err::BadParam;
  if (sh->len.load(std::memory_order_acquire) == 0)
    return Err::BadParam;
  payload(sh)[0] ^= 0xFFu;
  return Err::Ok;
}

} // namespace shmrq
