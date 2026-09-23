package tpc

import (
	"fmt"
	"os"
	"strings"
)

// Crash-injection hooks. A crash point is a named code location placed
// immediately AFTER a durable log write (fsync) and BEFORE the action that
// write permits (replying to an RPC, sending the next message). Arming a
// point makes the process exit there, simulating a crash at exactly that
// disk boundary.
//
// Arm points via SetCrashPoints (the binaries read TPC_CRASH_AFTER):
//
//	TPC_CRASH_AFTER=c:after-decision ./tpc-coordinator
//
// Coordinator points:
//
//	c:after-preparing     PREPARING fsynced, before replying to POST /tx
//	c:after-all-prepared  all yes votes collected, before writing the decision
//	c:after-decision      COMMIT/ABORT decision fsynced, before notifying participants
//	c:after-done          DONE fsynced
//
// Participant points:
//
//	p:after-prepared      PREPARED fsynced, before sending the yes vote
//	p:after-committed     COMMITTED fsynced, before acking
//	p:after-aborted       ABORTED fsynced, before acking
var crashSet = map[string]bool{}

// crashHook is invoked at every crash point (armed or not); tests use it to
// observe crash points in-process without exiting.
var crashHook = func(name string) {}

// SetCrashPoints arms a comma-separated list of crash points.
func SetCrashPoints(list string) {
	for _, p := range strings.Split(list, ",") {
		if p = strings.TrimSpace(p); p != "" {
			crashSet[p] = true
		}
	}
}

// CrashPoint triggers a simulated crash if name is armed.
func CrashPoint(name string) {
	crashHook(name)
	if crashSet[name] {
		fmt.Fprintf(os.Stderr, "SIMULATED CRASH at %s\n", name)
		os.Exit(2)
	}
}
