// Package worker holds the in-Job shell script used to produce real snapshot
// digests. Embedding it lets the controller create the Job without a mounted
// image and lets tests read the exact script that runs.
package worker

import _ "embed"

// Script is the shell program executed inside the worker container.
//
//go:embed worker.sh
var Script string
