// SPDX-License-Identifier: MIT
// shmring-producer — producer process driver for the SPSC queue.
//
//   shmring-producer NAME --capacity N --max-msg M --create
//                         (--gen COUNT [--gen-size S] | --stdin)
//                         [--timeout MS] [--flush] [--quiet]
//                         [--kill-at SEQ --kill-after-bytes B]
//
// Each committed message prints: OK seq=<n> len=<l>
// Exit status mirrors shmring::Status.
#include "cli_common.h"
#include "shmring.h"

#include <cstdio>
#include <iostream>
#include <string>
#include <unistd.h>

using namespace cli;

int main(int argc, char** argv) {
    Args a = parse_args(argc, argv);
    install_stop_handlers();
    if (a.pos.size() != 1) {
        std::fprintf(stderr, "usage: %s NAME --capacity N --max-msg M "
                             "(--gen COUNT | --stdin) [--create] ...\n",
                     argv[0]);
        return shmring::ERR_BADARG;
    }
    const std::string name = a.pos[0];
    uint64_t cap = a.getu("capacity", 0);
    uint64_t maxmsg = a.getu("max-msg", 0);
    bool create = a.has("create");
    int64_t timeout = a.geti("timeout", -1);
    bool flush = a.has("flush");
    bool quiet = a.has("quiet");
    uint64_t gen = a.getu("gen", 0);
    uint64_t gen_size = a.getu("gen-size", 64);
    int64_t kill_at = a.geti("kill-at", -1);
    int64_t kill_bytes = a.geti("kill-after-bytes", 1);
    bool use_stdin = a.has("stdin");
    if (!cap || !maxmsg || (!use_stdin && gen == 0) ||
        (use_stdin && gen != 0) ||
        (!use_stdin && gen_size > maxmsg))
        return shmring::ERR_BADARG;

    shmring::Queue q;
    if (create) {
        int rc = shmring::Queue::create(
            name, {static_cast<uint32_t>(cap),
                   static_cast<uint32_t>(maxmsg)}, q);
        if (rc != shmring::OK) {
            std::fprintf(stderr, "create failed: %s\n",
                         shmring::status_str(rc));
            return rc;
        }
        // Re-open as lease holder so this single process owns producer role.
        std::string n = name;
        q.close();
        rc = shmring::Queue::open(n, shmring::ROLE_PRODUCER, {}, q);
        if (rc != shmring::OK) return rc;
    } else {
        int rc = shmring::Queue::open(
            name, shmring::ROLE_PRODUCER,
            {static_cast<uint32_t>(cap), static_cast<uint32_t>(maxmsg)}, q);
        if (rc != shmring::OK) {
            std::fprintf(stderr, "open failed: %s\n",
                         shmring::status_str(rc));
            return rc;
        }
    }

    uint64_t produced = 0;
    int last_rc = shmring::OK;

    auto send = [&](const std::string& msg, uint64_t idx) {
        shmring::EnqueueOpts o;
        if (static_cast<int64_t>(idx) == kill_at) {
            o.kill_after_bytes = kill_bytes; // process dies inside enqueue()
        }
        int rc = q.enqueue(msg.data(), static_cast<uint32_t>(msg.size()),
                           timeout, &o);
        if (rc != shmring::OK) {
            last_rc = rc;
            std::fprintf(stderr, "enqueue seq? idx=%llu failed: %s\n",
                         static_cast<unsigned long long>(idx),
                         shmring::status_str(rc));
            return false;
        }
        ++produced;
        if (!quiet) {
            shmring::Info inf;
            q.info(inf);
            std::printf("OK seq=%llu len=%zu\n",
                        static_cast<unsigned long long>(inf.stats.tail - 1),
                        msg.size());
            if (flush) std::fflush(stdout);
        }
        return true;
    };

    for (uint64_t i = 0; i < gen; ++i) {
        // Sequence numbering is assigned by the queue; --gen only chooses
        // content. After a producer restart, content is regenerated from the
        // local loop index, which the kill test keeps disjoint.
        std::string msg = gen_payload(i, static_cast<uint32_t>(gen_size));
        if (!send(msg, i)) break;
    }

    if (use_stdin) {
        std::string line;
        uint64_t idx = 0;
        while (std::getline(std::cin, line)) {
            if (!send(line, idx++)) break;
        }
    }

    std::fflush(stdout);
    shmring::Info inf;
    q.info(inf);
    std::fprintf(stderr,
                 "producer stats: committed=%llu acked=%llu torn=%llu "
                 "full_waits=%llu\n",
                 static_cast<unsigned long long>(inf.stats.tail),
                 static_cast<unsigned long long>(inf.stats.head),
                 static_cast<unsigned long long>(inf.stats.torn_writes),
                 static_cast<unsigned long long>(inf.stats.full_waits));
    q.close();
    return last_rc;
}
