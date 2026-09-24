// Command orset-gc coordinates one safe tombstone-GC round over a set of
// running replicas, e.g.:
//
//	orset-gc -peers http://127.0.0.1:8081,http://127.0.0.1:8082,http://127.0.0.1:8083
//
// It fetches every replica's state, computes the intersection of tags that
// all replicas know as dead, and asks every replica to purge them. If any
// state fetch fails it aborts before purging anything (fail closed).
package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"net/http"
	"os"
	"strings"
	"time"

	"orset/internal/gc"
	"orset/internal/server"
)

func main() {
	peersFlag := flag.String("peers", "", "comma-separated base URLs of ALL live replicas")
	timeout := flag.Duration("timeout", 15*time.Second, "overall timeout")
	flag.Parse()

	if *peersFlag == "" {
		fmt.Fprintln(os.Stderr, "usage: orset-gc -peers http://host1,http://host2,...")
		os.Exit(2)
	}
	var peers []string
	for _, p := range strings.Split(*peersFlag, ",") {
		if p = strings.TrimSpace(p); p != "" {
			peers = append(peers, strings.TrimRight(p, "/"))
		}
	}

	httpc := &http.Client{Timeout: *timeout}
	clients := make([]gc.Client, len(peers))
	for i, p := range peers {
		clients[i] = server.NewGCClient(p, httpc)
	}

	ctx, cancel := context.WithTimeout(context.Background(), *timeout)
	defer cancel()
	rep, err := gc.Coordinate(ctx, clients, peers)
	if err != nil {
		fmt.Fprintf(os.Stderr, "gc failed: %v\n", err)
		os.Exit(1)
	}
	out, _ := json.MarshalIndent(map[string]any{
		"replicas":      rep.Replicas,
		"eligible":      rep.Eligible,
		"purgedPerNode": rep.Purged,
		"totalPurged":   rep.TagCount,
	}, "", "  ")
	fmt.Println(string(out))
}
