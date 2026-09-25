// Command cdag is the local content-driven build service.
//
// Usage:
//
//	cdag build   --file project.json [--cache-dir DIR] [--target id ...]
//	cdag serve   --cache-dir DIR [--addr 127.0.0.1:8080]
//	cdag validate --file project.json
//
// The cache directory is always separate from workspace directories; it
// defaults to a directory next to (never inside) the project file.
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"net/http"
	"os"
	"path/filepath"
	"time"

	"cdag/internal/cache"
	"cdag/internal/engine"
	"cdag/internal/server"
	"cdag/internal/spec"
)

func main() {
	if len(os.Args) < 2 {
		usage()
		os.Exit(2)
	}
	var err error
	switch os.Args[1] {
	case "build":
		err = runBuild(os.Args[2:])
	case "serve":
		err = runServe(os.Args[2:])
	case "validate":
		err = runValidate(os.Args[2:])
	case "-h", "--help", "help":
		usage()
		return
	default:
		usage()
		err = fmt.Errorf("unknown subcommand %q", os.Args[1])
	}
	if err != nil {
		fmt.Fprintln(os.Stderr, "cdag:", err)
		os.Exit(1)
	}
}

func usage() {
	fmt.Fprint(os.Stderr, `cdag - local content-driven DAG build service

Usage:
  cdag build    --file project.json [--cache-dir DIR] [--target ID]...
  cdag serve    --cache-dir DIR [--addr 127.0.0.1:8080]
  cdag validate --file project.json
`)
}

func defaultCacheDir(specPath string) string {
	abs, err := filepath.Abs(specPath)
	if err != nil {
		return ".cdag-cache"
	}
	// Beside the project file, never inside a workspace.
	return filepath.Join(filepath.Dir(abs), ".cdag-cache")
}

func openCache(dir string) (*cache.Cache, error) {
	if dir == "" {
		return nil, fmt.Errorf("--cache-dir is required (or use --file to take the default)")
	}
	abs, err := filepath.Abs(dir)
	if err != nil {
		return nil, err
	}
	return cache.Open(abs)
}

func runBuild(args []string) error {
	fs := flag.NewFlagSet("build", flag.ContinueOnError)
	file := fs.String("file", "", "path to project JSON")
	cacheDir := fs.String("cache-dir", "", "cache root (default: .cdag-cache next to the project file)")
	timeout := fs.Duration("timeout", 2*time.Minute, "per-node command timeout")
	var targets multiFlag
	fs.Var(&targets, "target", "node id to build (repeatable; default: all nodes)")
	if err := fs.Parse(args); err != nil {
		return err
	}
	if *file == "" {
		return fmt.Errorf("--file is required")
	}
	p, err := spec.LoadFile(*file)
	if err != nil {
		return err
	}
	cd := *cacheDir
	if cd == "" {
		cd = defaultCacheDir(*file)
	}
	if err := ensureCacheOutside(p.Workdir, cd); err != nil {
		return err
	}
	store, err := openCache(cd)
	if err != nil {
		return err
	}
	eng := engine.New(store, engine.WithTimeout(*timeout))
	report, err := eng.Build(p, targets)
	if err != nil {
		return err
	}
	enc := json.NewEncoder(os.Stdout)
	enc.SetIndent("", "  ")
	if err := enc.Encode(report); err != nil {
		return err
	}
	if !report.Success {
		os.Exit(1)
	}
	return nil
}

func runServe(args []string) error {
	fs := flag.NewFlagSet("serve", flag.ContinueOnError)
	cacheDir := fs.String("cache-dir", "", "cache root (required)")
	addr := fs.String("addr", "127.0.0.1:8080", "listen address")
	if err := fs.Parse(args); err != nil {
		return err
	}
	store, err := openCache(*cacheDir)
	if err != nil {
		return err
	}
	srv := &http.Server{
		Addr:              *addr,
		Handler:           server.New(store).Routes(),
		ReadHeaderTimeout: 10 * time.Second,
	}
	fmt.Fprintf(os.Stderr, "cdag serve: listening on http://%s (cache %s)\n", *addr, store.Root())
	return srv.ListenAndServe()
}

func runValidate(args []string) error {
	fs := flag.NewFlagSet("validate", flag.ContinueOnError)
	file := fs.String("file", "", "path to project JSON")
	if err := fs.Parse(args); err != nil {
		return err
	}
	if *file == "" {
		return fmt.Errorf("--file is required")
	}
	p, err := spec.LoadFile(*file)
	if err != nil {
		return err
	}
	out := map[string]any{
		"valid":      true,
		"id":         p.ID,
		"workdir":    p.Workdir,
		"node_count": len(p.Graph.Nodes),
	}
	enc := json.NewEncoder(os.Stdout)
	enc.SetIndent("", "  ")
	return enc.Encode(out)
}

// ensureCacheOutside refuses to place the cache root inside the workspace,
// keeping cache and working directory strictly separated.
func ensureCacheOutside(workdir, cacheDir string) error {
	aw, err := filepath.Abs(workdir)
	if err != nil {
		return err
	}
	ac, err := filepath.Abs(cacheDir)
	if err != nil {
		return err
	}
	rel, err := filepath.Rel(aw, ac)
	if err != nil {
		return nil
	}
	if rel == "." || (len(rel) >= 1 && rel[0] != '.' && rel != "..") {
		return fmt.Errorf("cache dir %q must not be inside workdir %q", ac, aw)
	}
	return nil
}

type multiFlag []string

func (m *multiFlag) String() string { return fmt.Sprint(*m) }
func (m *multiFlag) Set(v string) error {
	*m = append(*m, v)
	return nil
}
