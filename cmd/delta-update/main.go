// Command delta-update is the local build-engineering service and command-line
// client for block-level artifact delta updates. It operates entirely on local
// files and a local content-addressed cache; it never contacts any cloud
// platform.
package main

import (
	"fmt"
	"os"
)

const usageText = `delta-update - local artifact delta-update tool

Usage:
  delta-update serve --addr :8080 --cache DIR --work DIR
  delta-update artifact put --cache DIR FILE
  delta-update delta --cache DIR --old REF --new REF [--block-size N] [--patch FILE]
  delta-update apply --cache DIR --target FILE --patch FILE
  delta-update inspect FILE

REF is either a 64-char sha256 hash already in the cache or an absolute path.

Fixture fault injection for "apply" (hard crashes are CLI-only, never over HTTP):
  DELTA_CRASH_STAGE=<stage>        hard os.Exit at stage
  DELTA_FAULT_STAGE=<stage>        recoverable injected failure at stage
  DELTA_FAULT_AFTER_BYTES=<n>      fire write-delta fault after n bytes
  DELTA_SIM_FREE_BYTES=<n>         force the space preflight to see n bytes
Stages: check-old, space, prepare, write-delta, sync, verify-delta,
        rename, post-rename, fsync-dir, verify-new
`

func main() {
	args := os.Args[1:]
	if len(args) == 0 || args[0] == "-h" || args[0] == "--help" || args[0] == "help" {
		fmt.Fprint(os.Stdout, usageText)
		return
	}
	var err error
	switch args[0] {
	case "serve":
		err = runServe(args[1:])
	case "artifact":
		if len(args) < 2 || args[1] != "put" {
			err = fmt.Errorf("usage: artifact put --cache DIR FILE")
			break
		}
		err = runArtifactPut(args[2:])
	case "delta":
		err = runDelta(args[1:])
	case "apply":
		err = runApply(args[1:])
	case "inspect":
		err = runInspect(args[1:])
	default:
		err = fmt.Errorf("unknown subcommand %q\n\n%s", args[0], usageText)
	}
	if err != nil {
		fmt.Fprintf(os.Stderr, "error: %v\n", err)
		os.Exit(1)
	}
}
