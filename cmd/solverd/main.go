// Command solverd runs the dependency-resolver HTTP JSON service on a
// local address. It serves no frontend and makes no outbound network
// connections: registries arrive in request bodies, and the solve cache
// (when enabled) lives under the user cache directory, never the working
// directory.
package main

import (
	"flag"
	"log"
	"net/http"

	"depresolve/internal/api"
	"depresolve/internal/cache"
)

func main() {
	addr := flag.String("addr", "127.0.0.1:8080", "listen address")
	noCache := flag.Bool("no-cache", false, "disable the on-disk solve cache")
	cacheDir := flag.String("cache-dir", "", "cache directory (default: user cache dir/depresolve)")
	flag.Parse()

	var c *cache.Cache
	if !*noCache {
		dir := *cacheDir
		if dir == "" {
			d, err := cache.DefaultDir()
			if err != nil {
				log.Fatal(err)
			}
			dir = d
		}
		var err error
		c, err = cache.Open(dir)
		if err != nil {
			log.Fatal(err)
		}
		log.Printf("solve cache: %s", dir)
	}

	srv := api.NewServer(c)
	log.Printf("depresolve listening on http://%s", *addr)
	log.Printf("endpoints: GET /healthz, POST /v1/solve")
	if err := http.ListenAndServe(*addr, srv.Handler()); err != nil {
		log.Fatal(err)
	}
}
