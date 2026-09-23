// Command tracegen writes the deterministic Figure 8 fault traces (correct
// and deliberately buggy variants) to JSON files. The files are replayable
// fixtures usable with `cmd/verify`, the HTTP POST /v1/replay endpoint, or
// any check.Runner.
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"path/filepath"

	"raftlab/internal/check"
)

func main() {
	dir := flag.String("out", "testdata", "output directory")
	flag.Parse()
	if err := os.MkdirAll(*dir, 0o755); err != nil {
		fatal(err)
	}
	write := func(name string, buggy bool) {
		actions, err := check.Figure8Actions(buggy)
		if err != nil {
			fatal(err)
		}
		b, err := json.MarshalIndent(actions, "", "  ")
		if err != nil {
			fatal(err)
		}
		path := filepath.Join(*dir, name)
		if err := os.WriteFile(path, b, 0o644); err != nil {
			fatal(err)
		}
		fmt.Printf("wrote %s (%d actions)\n", path, len(actions))
	}
	write("figure8-correct.json", false)
	write("figure8-buggy.json", true)
}

func fatal(err error) {
	fmt.Fprintln(os.Stderr, "fatal:", err)
	os.Exit(1)
}
