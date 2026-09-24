// SPDX-License-Identifier: MIT
// shmring.h — SPSC cross-process ring queue over POSIX shared memory.
//
// Fixed-capacity queue of fixed-size slots carrying variable-length messages
// up to a compile-time-bounded maximum fixed at creation time.
#pragma once

#include <cstdint>
#include <cstddef>
#include <string>

namespace shmring {

// Library status codes. Process exit codes of the CLI tools reuse these values.
enum Status : int {
    OK = 0,
    ERR_FULL = 1,         // queue full, non-blocking enqueue rejected
    ERR_EMPTY = 2,        // queue empty, non-blocking dequeue
    ERR_TIMEOUT = 3,      // blocking wait exceeded the deadline
    ERR_TOO_LARGE = 4,    // message larger than max_msg (or read buffer too small)
    ERR_OVERFLOW = 5,     // 64-bit absolute sequence space exhausted
    ERR_CORRUPT = 6,      // CRC/identity check failed; queue MUST be re-created
    ERR_MISMATCH = 7,     // attaching with config different from creation
    ERR_NOMEM = 8,        // allocation / shm truncation failure
    ERR_SYS = 9,          // unexpected syscall error (see errno)
    ERR_INTERRUPTED = 10, // blocking wait cancelled by request_stop()
    ERR_BUSY = 11,        // another live process already owns this role
    ERR_BADARG = 12,
    ERR_NOTFOUND = 13,
};

const char* status_str(int s) noexcept;

struct Config {
    uint32_t capacity = 0; // number of slots, fixed at creation
    uint32_t max_msg = 0;  // maximum payload bytes per message, fixed at creation
};

struct Stats {
    uint64_t head = 0;          // total acknowledged (consumed) messages
    uint64_t tail = 0;          // total committed (produced) messages
    uint64_t torn_writes = 0;   // half-written slots recovered from dead producers
    uint64_t full_waits = 0;    // producer futex sleep episodes
    uint64_t empty_waits = 0;   // consumer futex sleep episodes
    uint64_t corrupt_events = 0;
};

struct Info {
    Config cfg;
    Stats stats;
    uint64_t size_bytes = 0;
    uint64_t slot_stride = 0;
    bool created_here = false;
};

enum Role : int {
    ROLE_NONE = 0,     // inspection (info/doctor/damage): takes no lease
    ROLE_PRODUCER = 1, // holds the producer lease for the process lifetime
    ROLE_CONSUMER = 2, // holds the consumer lease for the process lifetime
};

struct EnqueueOpts {
    // Test hook: after claiming the slot (state=WRITING) and copying at most
    // kill_after_bytes payload bytes, kill the current process with SIGKILL,
    // leaving a deterministic half-written slot. -1 disables.
    int64_t kill_after_bytes = -1;
};

struct DequeueOpts {
    // Test hook: after flipping state to READING and copying at most
    // kill_after_read_bytes bytes, kill the process with SIGKILL before the
    // acknowledgement (head advance), forcing a redelivery on restart.
    int64_t kill_after_read_bytes = -1;
};

class Queue {
public:
    Queue();
    ~Queue();

    Queue(const Queue&) = delete;
    Queue& operator=(const Queue&) = delete;
    Queue(Queue&& other) noexcept;
    Queue& operator=(Queue&& other) noexcept;

    // Create a brand-new queue (fails if the shm object already exists).
    // capacity in [2, 4_000_000], max_msg in [0, 2^24].
    static int create(const std::string& name, const Config& cfg, Queue& out);

    // Attach to an existing queue. The lease of `role` is taken for the
    // lifetime of this object; a dead previous owner triggers recovery.
    // `expected` fields may be 0 to skip that field's config check.
    static int open(const std::string& name, Role role,
                    const Config& expected, Queue& out);

    // Unlink the shm object name (mapping survives until all fds close).
    static int destroy(const std::string& name) noexcept;

    // All multi-process atomics must be lock-free; report whether they are.
    static bool runtime_lock_free() noexcept;

    // Enqueue one message. timeout_ms: -1 block forever, 0 non-blocking,
    // >0 maximum wait in milliseconds when full.
    int enqueue(const void* data, uint32_t len, int64_t timeout_ms,
                const EnqueueOpts* opts = nullptr);

    // Dequeue the next message in FIFO order into buf (capacity buf_cap).
    // On success out_len/out_seq hold payload length and absolute sequence
    // number (starting at 0, monotonic across wrap-around and restarts).
    int dequeue(void* buf, uint32_t buf_cap, uint32_t& out_len,
                uint64_t& out_seq, int64_t timeout_ms,
                const DequeueOpts* opts = nullptr);

    int info(Info& out) const noexcept;
    // CRC-audit every committed-but-unconsumed slot. Returns bad count.
    int doctor(uint64_t& bad_slots) const noexcept;
    // Test support: overwrite one byte of a committed slot's payload.
    int damage_committed(uint64_t seq, uint32_t offset, uint8_t value) noexcept;

    // Release the lease and mapping. Idempotent.
    void close() noexcept;

private:
    struct Impl;
    Impl* p_ = nullptr;
};

// Signals blocked enqueue/dequeue calls in *all* queues of this process to
// return ERR_INTERRUPTED. Installed by CLI signal handlers.
void request_stop() noexcept;

} // namespace shmring
