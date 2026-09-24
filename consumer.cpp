// SPDX-License-Identifier: MIT
// shmring-consumer — consumer process driver for the SPSC queue.
//
//   shmring-consumer NAME --capacity N --max-msg M
//                         [--count C] [--timeout MS] [--quiet]
//                         [--kill-at SEQ --kill-after-bytes B]
//
// Each delivered message prints: MSG seq=<n> len=<l> data=<escaped>
// (escaping: JSON-style, valid UTF-8 bytes passed through).
// Exit status mirrors shmring::Status; ERR_TIMEOUT (3) means the requested
// count was not reached before the deadline.
#include "cli_common.h"
#include "shmring.h"

#include <cstdio>
#include <string>
#include <vector>

using namespace cli;

int main(int argc, char** argv) {
    Args a = parse_args(argc, argv);
    install_stop_handlers();
    if (a.pos.size() != 1) {
        std::fprintf(stderr,
                     "usage: %s NAME --capacity N --max-msg M "
                     "[--count C] [--timeout MS] [--quiet]\n",
                     argv[0]);
        return shmring::ERR_BADARG;
    }
    const std::string name = a.pos[0];
    uint64_t cap = a.getu("capacity", 0);
    uint64_t maxmsg = a.getu("max-msg", 0);
    int64_t count = a.geti("count", -1);   // -1: run until interrupted
    int64_t timeout = a.geti("timeout", -1);
    bool quiet = a.has("quiet");
    int64_t kill_at = a.geti("kill-at", -1);
    int64_t kill_bytes = a.geti("kill-after-bytes", 0);
    if (!cap || !maxmsg) return shmring::ERR_BADARG;

    shmring::Queue q;
    int rc = shmring::Queue::open(
        name, shmring::ROLE_CONSUMER,
        {static_cast<uint32_t>(cap), static_cast<uint32_t>(maxmsg)}, q);
    if (rc != shmring::OK) {
        std::fprintf(stderr, "open failed: %s\n", shmring::status_str(rc));
        return rc;
    }

    std::vector<uint8_t> buf(static_cast<size_t>(maxmsg));
    uint64_t got = 0;
    int last_rc = shmring::OK;
    while (count < 0 || static_cast<uint64_t>(count) > got) {
        uint32_t len = 0;
        uint64_t seq = 0;
        shmring::DequeueOpts o;
        if (static_cast<int64_t>(got) == kill_at)
            o.kill_after_read_bytes = kill_bytes; // dies inside dequeue()
        rc = q.dequeue(buf.data(), static_cast<uint32_t>(buf.size()), len, seq,
                       timeout, &o);
        if (rc != shmring::OK) {
            last_rc = rc;
            if (rc == shmring::ERR_INTERRUPTED) break;
            std::fprintf(stderr, "dequeue failed: %s\n",
                         shmring::status_str(rc));
            break;
        }
        ++got;
        if (!quiet) {
            std::string out = "MSG seq=" + std::to_string(seq) +
                              " len=" + std::to_string(len) + " data=";
            for (uint32_t i = 0; i < len; ++i)
                append_escaped(out, buf[i]);
            emit_line(out);
        }
    }

    std::fflush(stdout);
    shmring::Info inf;
    q.info(inf);
    std::fprintf(stderr,
                 "consumer stats: received=%llu committed=%llu acked=%llu "
                 "empty_waits=%llu corrupt=%llu\n",
                 static_cast<unsigned long long>(got),
                 static_cast<unsigned long long>(inf.stats.tail),
                 static_cast<unsigned long long>(inf.stats.head),
                 static_cast<unsigned long long>(inf.stats.empty_waits),
                 static_cast<unsigned long long>(inf.stats.corrupt_events));
    q.close();
    return last_rc;
}
