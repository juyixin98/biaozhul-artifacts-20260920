// Command depresolve solves one request from a JSON file and prints the
// JSON result. It runs entirely offline against the registry embedded in
// the request file.
//
// Usage:
//
//	depresolve -req request.json [-no-cache] [-cache-dir DIR]
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"os"

	"depresolve/internal/api"
	"depresolve/internal/cache"
	"depresolve/internal/solver"
)

func main() {
	reqPath := flag.String("req", "", "path to a solve-request JSON file")
	noCache := flag.Bool("no-cache", false, "disable the on-disk solve cache")
	cacheDir := flag.String("cache-dir", "", "cache directory (default: user cache dir/depresolve)")
	flag.Parse()
	if *reqPath == "" {
		fmt.Fprintln(os.Stderr, "missing -req: path to a solve-request JSON file")
		os.Exit(2)
	}

	var req api.SolveRequest
	data, err := os.ReadFile(*reqPath)
	if err != nil {
		fail(err)
	}
	if err := json.Unmarshal(data, &req); err != nil {
		fail(fmt.Errorf("invalid request file: %w", err))
	}
	if len(req.Registry) == 0 {
		fail(fmt.Errorf("registry must be non-empty"))
	}
	if len(req.Root) == 0 {
		fail(fmt.Errorf("root must list at least one package"))
	}

	var c *cache.Cache
	if !*noCache && !req.Options.DisableCache {
		dir := *cacheDir
		if dir == "" {
			d, err := cache.DefaultDir()
			if err != nil {
				fail(err)
			}
			dir = d
		}
		c, err = cache.Open(dir)
		if err != nil {
			fail(err)
		}
	}

	key := ""
	if c != nil {
		k, err := cache.Key(req)
		if err != nil {
			fail(err)
		}
		key = k
		if raw, ok := c.Get(key); ok {
			var cached solver.Result
			if err := json.Unmarshal(raw, &cached); err == nil {
				emit(api.SolveResponse{Result: cached, Cached: true})
				return
			}
		}
	}

	sol, err := solver.NewSolver(req.Registry, req.Root)
	if err != nil {
		fail(err)
	}
	result := sol.Solve()
	if c != nil {
		stored, err := api.MarshalJSON(result)
		if err != nil {
			fail(err)
		}
		_ = c.Put(key, stored)
	}
	emit(api.SolveResponse{Result: result, Cached: false})
}

func emit(v any) {
	out, err := api.MarshalJSON(v)
	if err != nil {
		fail(err)
	}
	fmt.Println(string(out))
}

func fail(err error) {
	fmt.Fprintln(os.Stderr, "depresolve:", err)
	os.Exit(1)
}
