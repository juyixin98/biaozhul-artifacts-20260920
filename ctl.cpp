// SPDX-License-Identifier: MIT
// shmring-ctl — administrative / inspection driver.
//
//   shmring-ctl create   NAME --capacity N --max-msg M
//   shmring-ctl destroy  NAME
//   shmring-ctl info     NAME
//   shmring-ctl doctor   NAME            (CRC-audit queued messages)
//   shmring-ctl check                      (runtime lock-free assertion)
//   shmring-ctl damage   NAME --seq S --offset O --value V   (test only)
#include "cli_common.h"
#include "shmring.h"

#include <atomic>
#include <cstdint>
#include <cstdio>
#include <string>

using namespace cli;

static int cmd_create(const Args& a) {
    if (a.pos.size() != 2) return shmring::ERR_BADARG;
    uint64_t cap = a.getu("capacity", 0);
    uint64_t maxmsg = a.getu("max-msg", 0);
    if (!cap || !maxmsg) return shmring::ERR_BADARG;
    shmring::Queue q;
    int rc = shmring::Queue::create(
        a.pos[1], {static_cast<uint32_t>(cap),
                   static_cast<uint32_t>(maxmsg)}, q);
    if (rc != shmring::OK) {
        std::fprintf(stderr, "create failed: %s\n", shmring::status_str(rc));
        return rc;
    }
    shmring::Info inf;
    q.info(inf);
    std::printf("created name=%s capacity=%u max_msg=%u size=%llu\n",
                a.pos[1].c_str(), inf.cfg.capacity, inf.cfg.max_msg,
                static_cast<unsigned long long>(inf.size_bytes));
    return shmring::OK;
}

static int cmd_destroy(const Args& a) {
    if (a.pos.size() != 2) return shmring::ERR_BADARG;
    int rc = shmring::Queue::destroy(a.pos[1]);
    if (rc != shmring::OK)
        std::fprintf(stderr, "destroy failed: %s\n",
                     shmring::status_str(rc));
    else
        std::printf("destroyed name=%s\n", a.pos[1].c_str());
    return rc;
}

static int cmd_info(const Args& a) {
    if (a.pos.size() != 2) return shmring::ERR_BADARG;
    shmring::Queue q;
    int rc = shmring::Queue::open(a.pos[1], shmring::ROLE_NONE, {}, q);
    if (rc != shmring::OK) {
        std::fprintf(stderr, "open failed: %s\n", shmring::status_str(rc));
        return rc;
    }
    shmring::Info inf;
    q.info(inf);
    std::printf("name=%s\ncapacity=%u\nmax_msg=%u\nsize_bytes=%llu\n"
                "slot_stride=%llu\nhead=%llu\ntail=%llu\nqueued=%llu\n"
                "torn_writes=%llu\nfull_waits=%llu\nempty_waits=%llu\n"
                "corrupt_events=%llu\n",
                a.pos[1].c_str(), inf.cfg.capacity, inf.cfg.max_msg,
                static_cast<unsigned long long>(inf.size_bytes),
                static_cast<unsigned long long>(inf.slot_stride),
                static_cast<unsigned long long>(inf.stats.head),
                static_cast<unsigned long long>(inf.stats.tail),
                static_cast<unsigned long long>(inf.stats.tail -
                                                inf.stats.head),
                static_cast<unsigned long long>(inf.stats.torn_writes),
                static_cast<unsigned long long>(inf.stats.full_waits),
                static_cast<unsigned long long>(inf.stats.empty_waits),
                static_cast<unsigned long long>(inf.stats.corrupt_events));
    return shmring::OK;
}

static int cmd_doctor(const Args& a) {
    if (a.pos.size() != 2) return shmring::ERR_BADARG;
    shmring::Queue q;
    int rc = shmring::Queue::open(a.pos[1], shmring::ROLE_NONE, {}, q);
    if (rc != shmring::OK) return rc;
    uint64_t bad = 0;
    rc = q.doctor(bad);
    if (rc != shmring::OK) {
        std::fprintf(stderr, "doctor: structurally inconsistent queue; "
                             "re-create required\n");
        return shmring::ERR_CORRUPT;
    }
    std::printf("doctor: bad_slots=%llu %s\n",
                static_cast<unsigned long long>(bad),
                bad ? "CORRUPT: re-create required" : "healthy");
    return bad ? shmring::ERR_CORRUPT : shmring::OK;
}

static int cmd_check() {
    bool lf = shmring::Queue::runtime_lock_free();
    std::printf("atomic<uint32_t> lock_free: %s\natomic<uint64_t> lock_free: %s\n",
                std::atomic<uint32_t>::is_always_lock_free ? "yes" : "NO",
                std::atomic<uint64_t>::is_always_lock_free ? "yes" : "NO");
    if (!lf) {
        std::fprintf(stderr, "process-shared atomics are NOT lock-free on "
                             "this platform\n");
        return shmring::ERR_SYS;
    }
    return shmring::OK;
}

static int cmd_damage(const Args& a) {
    if (a.pos.size() != 2) return shmring::ERR_BADARG;
    uint64_t seq = a.getu("seq", UINT64_MAX);
    uint64_t off = a.getu("offset", 0);
    uint64_t val = a.getu("value", 0xFF);
    shmring::Queue q;
    int rc = shmring::Queue::open(a.pos[1], shmring::ROLE_NONE, {}, q);
    if (rc != shmring::OK) return rc;
    rc = q.damage_committed(seq, static_cast<uint32_t>(off),
                            static_cast<uint8_t>(val));
    if (rc != shmring::OK)
        std::fprintf(stderr, "damage failed: %s\n", shmring::status_str(rc));
    else
        std::printf("damaged seq=%llu offset=%llu value=%llu\n",
                    static_cast<unsigned long long>(seq),
                    static_cast<unsigned long long>(off),
                    static_cast<unsigned long long>(val));
    return rc;
}

int main(int argc, char** argv) {
    Args a = parse_args(argc, argv);
    if (a.pos.empty()) {
        std::fprintf(stderr,
                     "usage: %s {create|destroy|info|doctor|check|damage} "
                     "NAME ...\n",
                     argv[0]);
        return shmring::ERR_BADARG;
    }
    const std::string& cmd = a.pos[0];
    // keep "NAME" out of the option parsing position vector confusion
    Args shifted = a;
    if (cmd == "create") return cmd_create(shifted);
    if (cmd == "destroy") return cmd_destroy(shifted);
    if (cmd == "info") return cmd_info(shifted);
    if (cmd == "doctor") return cmd_doctor(shifted);
    if (cmd == "check") return cmd_check();
    if (cmd == "damage") return cmd_damage(shifted);
    std::fprintf(stderr, "unknown command: %s\n", cmd.c_str());
    return shmring::ERR_BADARG;
}
