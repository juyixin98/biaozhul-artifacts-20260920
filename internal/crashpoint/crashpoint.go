// Package crashpoint provides process kills at named points in the 2PC
// protocol. A point is armed via the TPC_CRASH environment variable:
//
//	TPC_CRASH=P2 ./...   kill the process (exit 42) the first time point
//	                     "P2" is reached
//
// Tests restart the killed node with TPC_CRASH unset ("crash once, then come
// back clean"). Each protocol point is visited at most once per transaction,
// so a plain point name is an unambiguous trigger.
package crashpoint

import "os"

// ExitCode is the status a node exits with when a crash point fires.
const ExitCode = 42

// Hit kills the process immediately when point equals TPC_CRASH. os.Exit does
// not run defers: buffered HTTP responses and in-memory state are lost, as in
// a power failure.
//
// Every call site sits right AFTER the corresponding WAL Append returned, so
// the durable/not-durable boundary is exactly the log boundary.
func Hit(point string) {
	if os.Getenv("TPC_CRASH") == point {
		os.Exit(ExitCode)
	}
}
