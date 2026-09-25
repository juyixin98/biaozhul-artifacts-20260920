// Command patchd runs the local patch-context application service and a
// command-line client for its batch apply operation.
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"time"

	"patchd/internal/batch"
	"patchd/internal/server"
)

func main() {
	if len(os.Args) < 2 {
		usage()
		os.Exit(2)
	}
	var err error
	switch os.Args[1] {
	case "serve":
		err = cmdServe(os.Args[2:])
	case "apply":
		err = cmdApply(os.Args[2:], false)
	case "validate":
		err = cmdApply(os.Args[2:], true)
	case "help", "-h", "--help":
		usage()
		return
	default:
		usage()
		os.Exit(2)
	}
	if err != nil {
		fmt.Fprintln(os.Stderr, "patchd:", err)
		os.Exit(1)
	}
}

func usage() {
	fmt.Fprint(os.Stderr, `patchd - local patch-context application service

Usage:
  patchd serve     [--addr 127.0.0.1:8080] [--cache-dir DIR]
  patchd apply     [--workdir DIR] [--cache-dir DIR] [--request FILE]
  patchd validate  [--workdir DIR] [--cache-dir DIR] [--request FILE]

Request files contain: {"patches":[{"diff":"...","encoding":"utf-8|base64"}]}.
With no --request, the request JSON is read from stdin.
`)
}

func defaultCache() string {
	base, err := os.UserCacheDir()
	if err != nil || base == "" {
		base = filepath.Join(os.TempDir(), "patchd-cache")
	}
	return filepath.Join(base, "patchd")
}

type fileRequest struct {
	Workdir string          `json:"workdir,omitempty"`
	DryRun  bool            `json:"dry_run,omitempty"`
	Patches []batch.Request `json:"patches"`
}

func cmdApply(args []string, dry bool) error {
	fs := flag.NewFlagSet("apply", flag.ContinueOnError)
	workdir := fs.String("workdir", "", "work directory to patch (required)")
	cacheDir := fs.String("cache-dir", defaultCache(), "staging cache directory (kept separate from workdir)")
	requestFile := fs.String("request", "", "request JSON file (default: stdin)")
	if err := fs.Parse(args); err != nil {
		return err
	}
	var input []byte
	var err error
	if *requestFile == "" || *requestFile == "-" {
		input, err = io.ReadAll(os.Stdin)
	} else {
		input, err = os.ReadFile(*requestFile)
	}
	if err != nil {
		return err
	}
	var fr fileRequest
	if err := json.Unmarshal(input, &fr); err != nil {
		return fmt.Errorf("invalid request JSON: %w", err)
	}
	if *workdir == "" {
		*workdir = fr.Workdir
	}
	if *workdir == "" {
		return fmt.Errorf("--workdir (or \"workdir\" in the request) is required")
	}
	absWork, err := filepath.Abs(*workdir)
	if err != nil {
		return err
	}
	cacheFor := filepath.Join(*cacheDir, safeDirName(absWork))
	plan, perr := batch.PlanBatch(absWork, cacheFor, fr.Patches)
	if perr != nil {
		return emitFailure(perr)
	}
	if dry || fr.DryRun {
		printResult(map[string]any{"status": "validated", "results": plan.Results})
		return nil
	}
	if perr := plan.Commit(); perr != nil {
		return emitFailure(perr)
	}
	printResult(map[string]any{"status": "applied", "results": plan.Results})
	return nil
}

func printResult(v any) {
	out, _ := json.MarshalIndent(v, "", "  ")
	fmt.Println(string(out))
}

func emitFailure(err error) error {
	out, _ := json.MarshalIndent(map[string]any{"status": "failed", "error": err}, "", "  ")
	fmt.Println(string(out))
	return fmt.Errorf("validation or publish failed")
}

func cmdServe(args []string) error {
	fs := flag.NewFlagSet("serve", flag.ContinueOnError)
	addr := fs.String("addr", "127.0.0.1:8080", "listen address (local only)")
	cacheDir := fs.String("cache-dir", defaultCache(), "staging cache directory")
	if err := fs.Parse(args); err != nil {
		return err
	}
	srv, err := server.New(server.Config{CacheDir: *cacheDir})
	if err != nil {
		return err
	}
	httpSrv := &http.Server{
		Addr:              *addr,
		Handler:           srv.Handler(),
		ReadHeaderTimeout: 10 * time.Second,
	}
	ln, err := net.Listen("tcp", *addr)
	if err != nil {
		return err
	}
	fmt.Fprintf(os.Stderr, "patchd listening on http://%s (cache: %s)\n", ln.Addr(), *cacheDir)
	return httpSrv.Serve(ln)
}

func safeDirName(p string) string {
	out := make([]byte, 0, len(p))
	for i := 0; i < len(p); i++ {
		c := p[i]
		switch {
		case c >= 'a' && c <= 'z', c >= 'A' && c <= 'Z', c >= '0' && c <= '9', c == '-', c == '_':
			out = append(out, c)
		default:
			out = append(out, '_')
		}
	}
	return string(out)
}
