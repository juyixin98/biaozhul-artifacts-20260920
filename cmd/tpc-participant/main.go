// Command tpc-participant runs one two-phase commit participant.
//
// Crash injection: set TPC_CRASH_AFTER to a comma-separated list of crash
// points (see crash.go), e.g. TPC_CRASH_AFTER=p:after-prepared.
package main

import (
	"flag"
	"log"
	"net/http"
	"os"

	"tpc"
)

func main() {
	addr := flag.String("addr", ":8081", "listen address")
	logPath := flag.String("log", "participant.wal", "write-ahead log file")
	coord := flag.String("coordinator", "http://localhost:8080", "coordinator base URL (used to resolve prepared transactions after a restart)")
	crash := flag.String("crash-after", os.Getenv("TPC_CRASH_AFTER"), "comma-separated crash points to arm")
	flag.Parse()

	tpc.SetCrashPoints(*crash)

	p, err := tpc.NewParticipant(*logPath, *coord, 0)
	if err != nil {
		log.Fatalf("open participant: %v", err)
	}
	log.Printf("participant listening on %s, wal=%s, coordinator=%s", *addr, *logPath, *coord)
	log.Fatal(http.ListenAndServe(*addr, p.Handler()))
}
