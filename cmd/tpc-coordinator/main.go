// Command tpc-coordinator runs the two-phase commit coordinator.
//
// Crash injection: set TPC_CRASH_AFTER to a comma-separated list of crash
// points (see crash.go) to make the process exit right after that durable
// log write, e.g. TPC_CRASH_AFTER=c:after-decision.
package main

import (
	"flag"
	"log"
	"net/http"
	"os"

	"tpc"
)

func main() {
	addr := flag.String("addr", ":8080", "listen address")
	logPath := flag.String("log", "coordinator.wal", "write-ahead log file")
	crash := flag.String("crash-after", os.Getenv("TPC_CRASH_AFTER"), "comma-separated crash points to arm")
	flag.Parse()

	tpc.SetCrashPoints(*crash)

	c, err := tpc.NewCoordinator(*logPath, 0)
	if err != nil {
		log.Fatalf("open coordinator: %v", err)
	}
	log.Printf("coordinator listening on %s, wal=%s", *addr, *logPath)
	log.Fatal(http.ListenAndServe(*addr, c.Handler()))
}
