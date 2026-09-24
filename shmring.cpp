// SPDX-License-Identifier: MIT
#include "shmring.h"

#include <atomic>
#include <cerrno>
#include <cstring>
#include <ctime>
#include <fcntl.h>
#include <signal.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <unistd.h>
#include <sched.h>
#include <pthread.h>

#include <new>
#include <string>

namespace shmring {

// ---------------------------------------------------------------------------
// On-disk / in-memory wire format.
//
// The mmap region is: [Header][capacity slots][slot_stride bytes each].
// Layout is frozen for MAGIC/VERSION; all multi-byte integers are little
// endian by convention (the host is assumed little endian, asserted at open).
// ---------------------------------------------------------------------------

static constexpr uint32_t MAGIC = 0x31474E52; // "RNG1" little endian
static constexpr uint32_t VERSION = 1;
static constexpr uint32_t MAX_CAPACITY = 4'000'000;
static constexpr uint32_t MAX_MSG_BYTES = 1u << 24; // 16 MiB

// Slot lifecycle.  State machine per slot, cycled in lock step with the
// producer tail (the state is the *crash commit marker* that distinguishes a
// published message from a half-written slot):
//
//   AVAILABLE --(producer claims slot)--> WRITING
//   WRITING   --(store-release crc/len/seq, then state)--> COMMITTED
//   COMMITTED --(consumer validates, marks reading)--> READING
//   READING   --(consumer store-release head, then state)--> AVAILABLE
//
// A producer that dies while WRITING leaves exactly one torn slot (the one at
// index tail % capacity). A consumer that dies while READING leaves at most one
// ack-window slot, which must be redelivered (at-least-once).
enum SlotState : uint32_t {
    ST_AVAILABLE = 0,
    ST_WRITING = 1,
    ST_COMMITTED = 2,
    ST_READING = 3,
};

struct Header {
    uint32_t magic;
    uint32_t version;
    uint32_t capacity;    // fixed
    uint32_t max_msg;     // fixed
    uint64_t slot_stride; // sizeof(Slot header) + payload_cap + padding
    uint64_t size_bytes;  // total mapped size
    uint64_t head;        // absolute sequence of next message to consume
    uint64_t tail;        // absolute sequence of next message to produce
    uint64_t torn_writes;
    uint64_t full_waits;
    uint64_t empty_waits;
    uint64_t corrupt_events;
    // futex words: producer waits on wake_free, consumer waits on wake_used.
    uint32_t wake_free;
    uint32_t wake_used;
    uint32_t reserved[2];
    // Robust, process-shared, PTHREAD_MUTEX_ROBUST role leases. The owner
    // thread holds the lease for the whole process lifetime.
    pthread_mutex_t producer_lock;
    pthread_mutex_t consumer_lock;
};

struct Slot {
    std::atomic<uint32_t> state; // SlotState
    uint32_t len;                // payload bytes, valid in COMMITTED/READING
    uint32_t crc;                // CRC32 over little-endian seq||len||payload
    uint32_t pad0;
    uint64_t seq;                // absolute sequence bound to the committed data
    // payload[0..len) follows within the same slot stride
    uint8_t payload[1];
};

// ---------------------------------------------------------------------------
// CRC32 (IEEE 802.3, reflected, init 0xFFFFFFFF, xorout 0xFFFFFFFF).
// Pure software table implementation — no external dependency. This detects
// torn writes / bit rot; it is integrity protection, not a cryptographic MAC.
// ---------------------------------------------------------------------------

namespace {

uint32_t crc32_table[256];

void crc32_init() {
    for (uint32_t i = 0; i < 256; ++i) {
        uint32_t c = i;
        for (int k = 0; k < 8; ++k)
            c = (c & 1) ? (0xEDB88320u ^ (c >> 1)) : (c >> 1);
        crc32_table[i] = c;
    }
}

struct CrcOnce {
    CrcOnce() { crc32_init(); }
} g_crc_once;

uint32_t crc32_buf(const void* p, size_t n, uint32_t crc) {
    const auto* b = static_cast<const uint8_t*>(p);
    while (n--)
        crc = crc32_table[(crc ^ *b++) & 0xFFu] ^ (crc >> 8);
    return crc;
}

uint32_t slot_crc(uint64_t seq, uint32_t len, const uint8_t* payload) {
    uint32_t crc = 0xFFFFFFFFu;
    for (int i = 0; i < 8; ++i)
        crc = crc32_table[(crc ^ static_cast<uint8_t>(seq >> (8 * i))) & 0xFFu] ^
              (crc >> 8);
    for (int i = 0; i < 4; ++i)
        crc = crc32_table[(crc ^ static_cast<uint8_t>(len >> (8 * i))) & 0xFFu] ^
              (crc >> 8);
    crc = crc32_buf(payload, len, crc);
    return crc ^ 0xFFFFFFFFu;
}

// ---------------------------------------------------------------------------
// Linux futex(FUTEX_WAIT_BITSET / WAKE_BITSET), private-within-mapping style.
// FUTEX_PRIVATE_FLAG must NOT be used: the word lives in a shared mapping.
// ---------------------------------------------------------------------------

#ifndef SYS_futex
#define SYS_futex __NR_futex
#endif
#ifndef FUTEX_WAIT_BITSET
#define FUTEX_WAIT_BITSET 9
#endif
#ifndef FUTEX_WAKE_BITSET
#define FUTEX_WAKE_BITSET 10
#endif
#ifndef FUTEX_BITSET_MATCH_ANY
#define FUTEX_BITSET_MATCH_ANY 0xFFFFFFFFu
#endif

int futex_wait_abs(volatile uint32_t* uaddr, uint32_t val,
                   const struct timespec* abstime) {
    return syscall(SYS_futex, reinterpret_cast<long>(uaddr),
                   FUTEX_WAIT_BITSET, static_cast<unsigned long>(val),
                   abstime, nullptr, FUTEX_BITSET_MATCH_ANY);
}

int futex_wake_one(volatile uint32_t* uaddr) {
    return syscall(SYS_futex, reinterpret_cast<long>(uaddr),
                   FUTEX_WAKE_BITSET, 1UL, nullptr, nullptr,
                   FUTEX_BITSET_MATCH_ANY);
}

void mono_now(struct timespec& ts) {
    clock_gettime(CLOCK_MONOTONIC, &ts);
}

void mono_add(struct timespec& ts, uint32_t ms) {
    ts.tv_sec += ms / 1000;
    long n = ts.tv_nsec + static_cast<long>(ms % 1000) * 1'000'000L;
    if (n >= 1'000'000'000L) {
        ts.tv_sec += 1;
        n -= 1'000'000'000L;
    }
    ts.tv_nsec = n;
}

std::atomic<bool> g_stop{false};

void cpu_relax() {
#if defined(__x86_64__) || defined(__i386__)
    __builtin_ia32_pause();
#elif defined(__aarch64__)
    asm volatile("yield" ::: "memory");
#else
    // Fallback still provides compiler ordering; scheduler does the rest.
    asm volatile("" ::: "memory");
#endif
}

// ---------------------------------------------------------------------------
// Robust process-shared mutex lease.
// ---------------------------------------------------------------------------

int mutex_init_robust(pthread_mutex_t* m) {
    pthread_mutexattr_t a;
    int rc = pthread_mutexattr_init(&a);
    if (rc) return rc;
    pthread_mutexattr_setpshared(&a, PTHREAD_PROCESS_SHARED);
    pthread_mutexattr_setrobust(&a, PTHREAD_MUTEX_ROBUST);
    rc = pthread_mutex_init(m, &a);
    pthread_mutexattr_destroy(&a);
    return rc;
}

// Locks a role lease.
//   Returns 0 on a clean lock;
//   EOWNERDEAD if the previous holder died (state was repaired by the
//              caller-supplied repair callback, then state_consistent);
//   EBUSY if another live holder exists (trylock);
//   other errno on failure.
template <class Repair>
int role_lock_take(pthread_mutex_t* m, bool blocking, Repair repair) {
    int rc = blocking ? pthread_mutex_lock(m) : pthread_mutex_trylock(m);
    if (rc == EOWNERDEAD) {
        bool consistent = true;
        repair(consistent);
        if (consistent) {
            pthread_mutex_consistent(m);
            return EOWNERDEAD;
        }
        pthread_mutex_unlock(m); // abandon; futex stays non-recoverable
        return ENOTRECOVERABLE;
    }
    return rc;
}

uint64_t slot_offset(const Header* h, uint64_t i) {
    return sizeof(Header) + i * h->slot_stride;
}

Slot* slot_at(Header* h, uint64_t i) {
    return reinterpret_cast<Slot*>(reinterpret_cast<uint8_t*>(h) +
                                   slot_offset(h, i));
}

const Slot* slot_at(const Header* h, uint64_t i) {
    return reinterpret_cast<const Slot*>(
        reinterpret_cast<const uint8_t*>(h) + slot_offset(h, i));
}

} // namespace

// ---------------------------------------------------------------------------
// Queue::Impl
// ---------------------------------------------------------------------------

struct Queue::Impl {
    std::string name;
    int fd = -1;
    Header* h = nullptr;
    Role role = ROLE_NONE;
    bool owns_mapping = false;
    uint64_t cached_head = 0; // producer-side read cache of head
    uint64_t cached_tail = 0; // consumer-side read cache of tail

    // Convenience atomic views over the POD fields of the shared header.
    std::atomic<uint64_t>* ahead() const {
        return reinterpret_cast<std::atomic<uint64_t>*>(&h->head);
    }
    std::atomic<uint64_t>* atail() const {
        return reinterpret_cast<std::atomic<uint64_t>*>(&h->tail);
    }
    std::atomic<uint32_t>* afree() const {
        return reinterpret_cast<std::atomic<uint32_t>*>(&h->wake_free);
    }
    std::atomic<uint32_t>* aused() const {
        return reinterpret_cast<std::atomic<uint32_t>*>(&h->wake_used);
    }

    void release_mapping() {
        if (h && owns_mapping) {
            munmap(h, h->size_bytes);
            h = nullptr;
        }
        if (fd >= 0) {
            ::close(fd);
            fd = -1;
        }
    }

    // Expected seq of slot i given current head/tail region. Only called in
    // repair paths, where no concurrent role owner can exist.
    void repair_producer(bool& consistent);
    void repair_consumer(bool& consistent);
    bool doctor_locked(uint64_t& bad) const;

    // Wait until pred() becomes true, the deadline passes, or stop requested.
    // Increments the matching wait counter once per futex sleep episode.
    template <class Pred>
    int wait_for(Pred pred, std::atomic<uint32_t>* futex_word,
                 uint64_t* wait_counter, int64_t timeout_ms);
};

const char* status_str(int s) noexcept {
    switch (s) {
    case OK: return "ok";
    case ERR_FULL: return "queue full (rejected)";
    case ERR_EMPTY: return "queue empty";
    case ERR_TIMEOUT: return "timed out";
    case ERR_TOO_LARGE: return "message too large";
    case ERR_OVERFLOW: return "sequence space exhausted";
    case ERR_CORRUPT: return "corrupt slot: re-create the queue";
    case ERR_MISMATCH: return "queue configuration mismatch";
    case ERR_NOMEM: return "out of shared memory";
    case ERR_SYS: return "system error";
    case ERR_INTERRUPTED: return "interrupted";
    case ERR_BUSY: return "role already held by a live process";
    case ERR_BADARG: return "bad argument";
    case ERR_NOTFOUND: return "not found";
    default: return "unknown";
    }
}

void request_stop() noexcept { g_stop.store(true, std::memory_order_release); }

Queue::Queue() : p_(new Impl()) {}
Queue::~Queue() {
    if (p_) {
        close();
        delete p_;
        p_ = nullptr;
    }
}
Queue::Queue(Queue&& o) noexcept : p_(o.p_) { o.p_ = new Impl(); }
Queue& Queue::operator=(Queue&& o) noexcept {
    if (this != &o) {
        close();
        delete p_;
        p_ = o.p_;
        o.p_ = new Impl();
    }
    return *this;
}

void Queue::close() noexcept {
    if (!p_ || !p_->h) return;
    // Dropping the role lock makes the lease available to a successor; if the
    // process itself exits while holding it, the kernel hands EOWNERDEAD to
    // the next locker instead.
    if (p_->role == ROLE_PRODUCER)
        pthread_mutex_unlock(&p_->h->producer_lock);
    else if (p_->role == ROLE_CONSUMER)
        pthread_mutex_unlock(&p_->h->consumer_lock);
    p_->role = ROLE_NONE;
    p_->release_mapping();
}

static bool little_endian_host() {
    uint16_t x = 1;
    return *reinterpret_cast<uint8_t*>(&x) == 1;
}

static void shm_name(const std::string& name, std::string& out) {
    out = "/shmring_" + name;
}

static bool valid_name(const std::string& name) {
    if (name.empty() || name.size() > 64) return false;
    for (char c : name)
        if (!((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
              (c >= '0' && c <= '9') || c == '_' || c == '-'))
            return false;
    return true;
}

static int map_shm(const std::string& name, size_t size, bool create,
                   int& out_fd, void*& out_map) {
    std::string sn;
    shm_name(name, sn);
    int flags = O_RDWR | O_CLOEXEC;
    if (create) flags |= O_CREAT | O_EXCL;
    int fd = shm_open(sn.c_str(), flags, 0600);
    if (fd < 0) {
        if (errno == EEXIST || errno == ENOENT) return errno;
        return ERR_SYS;
    }
    if (create) {
        if (ftruncate(fd, static_cast<off_t>(size)) != 0) {
            int e = errno;
            ::close(fd);
            shm_unlink(sn.c_str());
            errno = e;
            return (e == EINVAL || e == EFBIG) ? ERR_NOMEM : ERR_SYS;
        }
    } else {
        struct stat st;
        if (fstat(fd, &st) != 0 || static_cast<uint64_t>(st.st_size) < size) {
            ::close(fd);
            return ERR_MISMATCH; // region smaller than this layout expects
        }
    }
    void* map = mmap(nullptr, size, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    if (map == MAP_FAILED) {
        int e = errno;
        ::close(fd);
        if (create) shm_unlink(sn.c_str());
        errno = e;
        return ERR_SYS;
    }
    out_fd = fd;
    out_map = map;
    return 0;
}

int Queue::destroy(const std::string& name) noexcept {
    if (!valid_name(name)) return ERR_BADARG;
    std::string sn;
    shm_name(name, sn);
    if (shm_unlink(sn.c_str()) != 0)
        return errno == ENOENT ? ERR_NOTFOUND : ERR_SYS;
    return OK;
}

bool Queue::runtime_lock_free() noexcept {
    return std::atomic<uint32_t>::is_always_lock_free &&
           std::atomic<uint64_t>::is_always_lock_free;
}

int Queue::create(const std::string& name, const Config& cfg, Queue& out) {
    if (!valid_name(name)) return ERR_BADARG;
    if (cfg.capacity < 2 || cfg.capacity > MAX_CAPACITY ||
        cfg.max_msg > MAX_MSG_BYTES)
        return ERR_BADARG;
    if (!runtime_lock_free()) return ERR_SYS;
    if (!little_endian_host()) return ERR_SYS;
    if (out.p_->h) out.close();

    const uint64_t payload_cap = cfg.max_msg;
    const uint64_t stride =
        (offsetof(Slot, payload) + payload_cap + 7) & ~uint64_t(7);
    const uint64_t total = sizeof(Header) + stride * cfg.capacity;

    int fd = -1;
    void* map = nullptr;
    int rc = map_shm(name, static_cast<size_t>(total), true, fd, map);
    if (rc == EEXIST) return ERR_BUSY;
    if (rc != OK) return rc;

    auto* h = new (map) Header(); // zero-initialise
    h->magic = MAGIC;
    h->version = VERSION;
    h->capacity = cfg.capacity;
    h->max_msg = cfg.max_msg;
    h->slot_stride = stride;
    h->size_bytes = total;

    for (uint32_t i = 0; i < cfg.capacity; ++i) {
        Slot* s = slot_at(h, i);
        s->state.store(ST_AVAILABLE, std::memory_order_relaxed);
        s->len = 0;
        s->crc = 0;
        s->seq = 0;
    }
    if (mutex_init_robust(&h->producer_lock) != 0 ||
        mutex_init_robust(&h->consumer_lock) != 0) {
        munmap(h, total);
        ::close(fd);
        destroy(name);
        return ERR_SYS;
    }

    out.p_->name = name;
    out.p_->fd = fd;
    out.p_->h = h;
    out.p_->owns_mapping = true;
    out.p_->cached_head = 0;
    out.p_->cached_tail = 0;
    return OK;
}

void Queue::Impl::repair_producer(bool& consistent) {
    // The robust mutex guarantees the *previous producer* died. It can leave
    // at most one WRITING slot: the one at the tail cursor. Recovery is
    // deliberately cursor-local — the consumer role may still be alive (it
    // holds a separate lease) and its head cursor moves concurrently, so a
    // full-queue scan could falsely accuse live slots. A dead consumer's
    // READING slot is left untouched: the restarted consumer owns its own
    // redelivery (at-least-once), and the producer is never blocked by it —
    // a READING slot occupies a queued position (tail-head), and when the
    // consumer restarts it redelivers then acks, freeing space in order.
    Header* hd = h;
    uint64_t t = reinterpret_cast<std::atomic<uint64_t>*>(&hd->tail)
                     ->load(std::memory_order_acquire);
    uint64_t hd_head = reinterpret_cast<std::atomic<uint64_t>*>(&hd->head)
                           ->load(std::memory_order_acquire);
    if (hd_head > t) { consistent = false; return; }

    Slot* s = slot_at(hd, t % hd->capacity);
    uint32_t st = s->state.load(std::memory_order_acquire);
    switch (st) {
    case ST_AVAILABLE:
        break; // predecessor died between tail++ and claiming the next slot
    case ST_COMMITTED:
        // tail reached an already-published slot — structurally inconsistent.
        consistent = false;
        return;
    case ST_READING:
        // tail and head share a physical slot only when the queue is empty:
        // a dead consumer's ack-window slot is then exactly this slot, and
        // the restarted consumer (its own repair) redelivers it; the producer
        // must simply wait for space in order like any full-queue case.
        if (t != hd_head) { consistent = false; return; }
        break;
    case ST_WRITING:
        s->len = 0;
        s->crc = 0;
        s->seq = 0;
        // Discard the torn slot WITHOUT advancing tail: no message with that
        // sequence was ever committed, and the successor producer reuses the
        // same sequence number. No committed (confirmed) message is lost.
        s->state.store(ST_AVAILABLE, std::memory_order_release);
        reinterpret_cast<std::atomic<uint64_t>*>(&hd->torn_writes)
            ->fetch_add(1, std::memory_order_relaxed);
        break;
    default:
        consistent = false;
        return;
    }
}

void Queue::Impl::repair_consumer(bool& consistent) {
    // The robust mutex guarantees the *previous consumer* died. It can leave
    // at most one READING slot, the one at the head cursor, carrying a valid
    // committed message; flip it back to COMMITTED for redelivery. Cursor-
    // local for the same concurrency reason as repair_producer: the producer
    // lease is independent and the producer may be mid-write right now.
    Header* hd = h;
    uint64_t hd_head = reinterpret_cast<std::atomic<uint64_t>*>(&hd->head)
                           ->load(std::memory_order_acquire);
    uint64_t t = reinterpret_cast<std::atomic<uint64_t>*>(&hd->tail)
                     ->load(std::memory_order_acquire);
    if (hd_head > t) { consistent = false; return; }
    if (hd_head == t) return; // empty queue: nothing to repair

    Slot* s = slot_at(hd, hd_head % hd->capacity);
    uint32_t st = s->state.load(std::memory_order_acquire);
    switch (st) {
    case ST_COMMITTED:
        break; // predecessor died before (or without) entering READING
    case ST_READING:
        if (s->seq != hd_head) {
            consistent = false;
            return;
        }
        s->state.store(ST_COMMITTED, std::memory_order_release);
        aused()->fetch_add(1, std::memory_order_release);
        futex_wake_one(
            reinterpret_cast<volatile uint32_t*>(&h->wake_used));
        break;
    case ST_AVAILABLE:
    case ST_WRITING:
    default:
        // head promises an unconsumed message; an empty/writing slot here
        // means the structure is inconsistent and re-initialisation is needed.
        consistent = false;
        return;
    }
}

bool Queue::Impl::doctor_locked(uint64_t& bad) const {
    bad = 0;
    const Header* hd = h;
    uint64_t hd_head = reinterpret_cast<const std::atomic<uint64_t>*>(&hd->head)
                           ->load(std::memory_order_acquire);
    uint64_t t = reinterpret_cast<const std::atomic<uint64_t>*>(&hd->tail)
                     ->load(std::memory_order_acquire);
    if (hd_head > t) return false;
    for (uint64_t n = hd_head; n < t; ++n) {
        const Slot* s = slot_at(hd, n % hd->capacity);
        uint32_t st = s->state.load(std::memory_order_acquire);
        if (st != ST_COMMITTED && st != ST_READING) return false;
        if (s->seq != n || s->len > hd->max_msg) {
            ++bad;
            continue;
        }
        uint32_t want = slot_crc(n, s->len,
                                 const_cast<uint8_t*>(s->payload));
        if (want != s->crc) ++bad;
    }
    return true;
}

int Queue::open(const std::string& name, Role role, const Config& expected,
                Queue& out) {
    if (!valid_name(name)) return ERR_BADARG;
    if (!runtime_lock_free()) return ERR_SYS;
    if (!little_endian_host()) return ERR_SYS;
    if (out.p_->h) out.close();

    // Probe: map with just enough to read the header first, then remap with
    // the real size.
    std::string sn;
    shm_name(name, sn);
    int fd = shm_open(sn.c_str(), O_RDWR | O_CLOEXEC, 0);
    if (fd < 0) return errno == ENOENT ? ERR_NOTFOUND : ERR_SYS;
    struct stat st;
    if (fstat(fd, &st) != 0) {
        ::close(fd);
        return ERR_SYS;
    }
    if (static_cast<uint64_t>(st.st_size) < sizeof(Header)) {
        ::close(fd);
        return ERR_CORRUPT;
    }
    void* map = mmap(nullptr, static_cast<size_t>(st.st_size),
                     PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    if (map == MAP_FAILED) {
        ::close(fd);
        return ERR_SYS;
    }
    auto* h0 = static_cast<Header*>(map);
    if (h0->magic != MAGIC || h0->version != VERSION) {
        munmap(map, static_cast<size_t>(st.st_size));
        ::close(fd);
        return ERR_CORRUPT;
    }
    if (h0->slot_stride == 0 ||
        h0->size_bytes != static_cast<uint64_t>(st.st_size) ||
        h0->size_bytes !=
            sizeof(Header) + h0->slot_stride * h0->capacity ||
        h0->capacity < 2 || h0->capacity > MAX_CAPACITY ||
        h0->max_msg > MAX_MSG_BYTES) {
        munmap(map, static_cast<size_t>(st.st_size));
        ::close(fd);
        return ERR_CORRUPT;
    }
    if ((expected.capacity && expected.capacity != h0->capacity) ||
        (expected.max_msg && expected.max_msg != h0->max_msg)) {
        munmap(map, static_cast<size_t>(st.st_size));
        ::close(fd);
        return ERR_MISMATCH;
    }

    out.p_->name = name;
    out.p_->fd = fd;
    out.p_->h = h0;
    out.p_->owns_mapping = true;
    out.p_->cached_head = h0->head;
    out.p_->cached_tail = h0->head;

    if (role == ROLE_PRODUCER) {
        bool recovered = false;
        int rc = role_lock_take(
            &h0->producer_lock, true, [&](bool& ok) {
                out.p_->repair_producer(ok);
                recovered = ok;
            });
        if (rc == ENOTRECOVERABLE) {
            out.close();
            return ERR_CORRUPT;
        }
        if (rc != 0 && rc != EOWNERDEAD) {
            out.close();
            return ERR_SYS;
        }
        (void)recovered;
        out.p_->role = ROLE_PRODUCER;
        out.p_->cached_head = h0->head;
        out.p_->cached_tail = h0->tail;
    } else if (role == ROLE_CONSUMER) {
        int rc = role_lock_take(
            &h0->consumer_lock, true, [&](bool& ok) {
                out.p_->repair_consumer(ok);
            });
        if (rc == ENOTRECOVERABLE) {
            out.close();
            return ERR_CORRUPT;
        }
        if (rc != 0 && rc != EOWNERDEAD) {
            out.close();
            return ERR_SYS;
        }
        out.p_->role = ROLE_CONSUMER;
        out.p_->cached_tail = h0->tail;
    }
    return OK;
}

// ---------------------------------------------------------------------------
// Blocking: short adaptive spin, then futex waits in bounded chunks so that
// timeouts and request_stop() stay responsive even with infinite deadline.
// ---------------------------------------------------------------------------

template <class Pred>
int Queue::Impl::wait_for(Pred pred, std::atomic<uint32_t>* word,
                          uint64_t* wait_counter, int64_t timeout_ms) {
    constexpr int SPINS = 128;
    for (int i = 0; i < SPINS; ++i) {
        if (g_stop.load(std::memory_order_acquire)) return ERR_INTERRUPTED;
        if (pred()) return OK;
        cpu_relax();
    }
    if (pred()) return OK;

    const bool finite = timeout_ms >= 0;
    struct timespec deadline;
    if (finite) {
        mono_now(deadline);
        mono_add(deadline, static_cast<uint32_t>(timeout_ms));
    }
    constexpr uint32_t CHUNK_MS = 20;
    bool counted = false;
    for (;;) {
        if (g_stop.load(std::memory_order_acquire)) return ERR_INTERRUPTED;
        if (pred()) return OK;

        struct timespec until;
        if (finite) {
            struct timespec now;
            mono_now(now);
            if (now.tv_sec > deadline.tv_sec ||
                (now.tv_sec == deadline.tv_sec &&
                 now.tv_nsec >= deadline.tv_nsec))
                return ERR_TIMEOUT;
            until = now;
            mono_add(until, CHUNK_MS);
            if (until.tv_sec > deadline.tv_sec ||
                (until.tv_sec == deadline.tv_sec &&
                 until.tv_nsec > deadline.tv_nsec))
                until = deadline;
        } else {
            mono_now(until);
            mono_add(until, CHUNK_MS);
        }

        uint32_t val = word->load(std::memory_order_relaxed);
        if (!counted) {
            (*wait_counter)++;
            counted = true;
        }
        int rc = futex_wait_abs(
            reinterpret_cast<volatile uint32_t*>(word),
            val, &until);
        if (rc == 0 || errno == EAGAIN) {
            if (pred()) return OK;
        } else if (errno != EINTR && errno != ETIMEDOUT) {
            return ERR_SYS;
        }
        sched_yield();
    }
}

// ---------------------------------------------------------------------------
// Enqueue / dequeue.
//
// Memory-order contract (SPSC, cross-process via MAP_SHARED):
//   * Counters head/tail are std::atomic<uint64_t> (lock-free asserted).
//   * Producer publishes payload, len, crc, seq with relaxed stores, then
//     state=COMMITTED with RELEASE inside the slot, followed by tail++
//     RELEASE. Consumer takes an ACQUIRE on tail; the slot's matching
//     ACQUIRE on state synchronises the payload data.
//   * Consumer acknowledges with head++ RELEASE; producer ACQUIREs head and
//     the AVAILABLE state with ACQUIRE before reusing a slot. Therefore a
//     consumer can never observe payload bytes of a not-yet-committed slot,
//     and a producer can never overwrite an unacknowledged message.
// ---------------------------------------------------------------------------

int Queue::enqueue(const void* data, uint32_t len, int64_t timeout_ms,
                   const EnqueueOpts* opts) {
    if (!p_ || !p_->h || p_->role != ROLE_PRODUCER) return ERR_BADARG;
    Impl* q = p_;
    Header* h = q->h;
    if (len > h->max_msg) return ERR_TOO_LARGE;

    uint64_t t = q->cached_tail;
    auto* atail = q->atail();
    if (t != atail->load(std::memory_order_relaxed)) {
        t = atail->load(std::memory_order_relaxed); // first call after restart
        q->cached_tail = t;
    }
    if (t == UINT64_MAX) return ERR_OVERFLOW;

    // Full? tail - head == capacity.
    auto full_pred = [&] {
        uint64_t hd = q->cached_head;
        if (t - hd < h->capacity) return true;
        hd = q->ahead()->load(std::memory_order_acquire);
        q->cached_head = hd;
        return t - hd < h->capacity;
    };
    if (!full_pred()) {
        if (timeout_ms == 0) return ERR_FULL;
        int rc = q->wait_for(full_pred, q->afree(), &h->full_waits, timeout_ms);
        if (rc != OK) return rc;
    }

    Slot* s = slot_at(h, t % h->capacity);
    // Claim: state AVAILABLE -> WRITING. Relaxed: the tail/head relationship
    // already serialises single producer vs consumer here; state is the
    // recovery marker observed by future processes.
    s->state.store(ST_WRITING, std::memory_order_relaxed);

    int64_t kill_at = opts ? opts->kill_after_bytes : -1;
    if (kill_at >= 0) {
        uint32_t part = len;
        if (static_cast<uint64_t>(kill_at) < part)
            part = static_cast<uint32_t>(kill_at);
        if (part) memcpy(s->payload, data, part);
        // Deterministic half-written slot: die after a partial payload copy.
        raise(SIGKILL);
    }
    if (len) memcpy(s->payload, data, len);
    s->len = len;
    s->seq = t;
    s->crc = slot_crc(t, len, static_cast<uint8_t*>(s->payload));

    // Publish: RELEASE so all data stores happen-before COMMITTED visibility.
    s->state.store(ST_COMMITTED, std::memory_order_release);
    q->cached_tail = t + 1;
    atail->store(t + 1, std::memory_order_release);

    // Wake one consumer. Epoch bump first (release), then futex op.
    q->aused()->fetch_add(1, std::memory_order_release);
    futex_wake_one(reinterpret_cast<volatile uint32_t*>(&h->wake_used));
    return OK;
}

int Queue::dequeue(void* buf, uint32_t buf_cap, uint32_t& out_len,
                   uint64_t& out_seq, int64_t timeout_ms,
                   const DequeueOpts* opts) {
    if (!p_ || !p_->h || p_->role != ROLE_CONSUMER) return ERR_BADARG;
    Impl* q = p_;
    Header* h = q->h;

    uint64_t hdpos = q->cached_head; // consumer's acknowledgement cursor
    auto empty_pred = [&] {
        if (q->cached_tail != hdpos) return true;
        uint64_t t = q->atail()->load(std::memory_order_acquire);
        q->cached_tail = t;
        return t != hdpos;
    };
    if (!empty_pred()) {
        if (timeout_ms == 0) return ERR_EMPTY;
        int rc =
            q->wait_for(empty_pred, q->aused(), &h->empty_waits, timeout_ms);
        if (rc != OK) return rc;
    }

    Slot* s = slot_at(h, hdpos % h->capacity);
    uint32_t st = s->state.load(std::memory_order_acquire);
    if (st != ST_COMMITTED) {
        // A producer crash leaves WRITING here; a fresh producer recovers it
        // before touching tail. Seeing anything else here is corruption.
        return ERR_CORRUPT;
    }
    if (s->seq != hdpos) return ERR_CORRUPT; // double-wrap / torn marker
    uint32_t len = s->len;
    if (len > h->max_msg) return ERR_CORRUPT;
    uint32_t want = slot_crc(hdpos, len, s->payload);
    if (want != s->crc) {
        reinterpret_cast<std::atomic<uint64_t>*>(&h->corrupt_events)
            ->fetch_add(1, std::memory_order_relaxed);
        return ERR_CORRUPT;
    }
    if (len > buf_cap) return ERR_TOO_LARGE;

    // Mark the ack window. Stays COMMITTED-visible under recovery semantics:
    // if we die now, repair flips READING back to COMMITTED for redelivery.
    s->state.store(ST_READING, std::memory_order_relaxed);

    int64_t kill_at = opts ? opts->kill_after_read_bytes : -1;
    if (kill_at >= 0) {
        uint32_t part = len;
        if (static_cast<uint64_t>(kill_at) < part)
            part = static_cast<uint32_t>(kill_at);
        if (part) memcpy(buf, s->payload, part);
        raise(SIGKILL);
    }
    if (len) memcpy(buf, s->payload, len);
    out_len = len;
    out_seq = hdpos;

    // Acknowledge: advance head BEFORE recycling the slot. A consumer restart
    // resumes at the new head; producers only reuse after head release.
    q->ahead()->store(hdpos + 1, std::memory_order_release);
    s->len = 0;
    s->seq = 0;
    s->state.store(ST_AVAILABLE, std::memory_order_release);

    // cached_tail may lag by one; the next empty_pred() reloads it atomically.
    q->cached_head = hdpos + 1;

    q->afree()->fetch_add(1, std::memory_order_release);
    futex_wake_one(reinterpret_cast<volatile uint32_t*>(&h->wake_free));
    return OK;
}

int Queue::info(Info& out) const noexcept {
    if (!p_ || !p_->h) return ERR_BADARG;
    const Header* h = p_->h;
    out.cfg.capacity = h->capacity;
    out.cfg.max_msg = h->max_msg;
    out.slot_stride = h->slot_stride;
    out.size_bytes = h->size_bytes;
    out.stats.head =
        reinterpret_cast<const std::atomic<uint64_t>*>(&h->head)
            ->load(std::memory_order_acquire);
    out.stats.tail =
        reinterpret_cast<const std::atomic<uint64_t>*>(&h->tail)
            ->load(std::memory_order_acquire);
    out.stats.torn_writes = h->torn_writes;
    out.stats.full_waits = h->full_waits;
    out.stats.empty_waits = h->empty_waits;
    out.stats.corrupt_events = h->corrupt_events;
    return OK;
}

int Queue::doctor(uint64_t& bad_slots) const noexcept {
    if (!p_ || !p_->h) return ERR_BADARG;
    bad_slots = 0;
    return p_->doctor_locked(bad_slots) ? OK : ERR_CORRUPT;
}

int Queue::damage_committed(uint64_t seq, uint32_t offset,
                            uint8_t value) noexcept {
    if (!p_ || !p_->h) return ERR_BADARG;
    Header* h = p_->h;
    uint64_t hd = reinterpret_cast<std::atomic<uint64_t>*>(&h->head)
                      ->load(std::memory_order_acquire);
    uint64_t t = reinterpret_cast<std::atomic<uint64_t>*>(&h->tail)
                     ->load(std::memory_order_acquire);
    if (seq < hd || seq >= t) return ERR_NOTFOUND;
    Slot* s = slot_at(h, seq % h->capacity);
    if (s->state.load(std::memory_order_acquire) != ST_COMMITTED ||
        s->seq != seq)
        return ERR_NOTFOUND;
    if (offset >= s->len) return ERR_BADARG;
    s->payload[offset] = value;
    return OK;
}

} // namespace shmring
