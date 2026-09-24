// Command merklesync provides the anti-entropy Merkle-sync service.
//
// Subcommands:
//
//	merklesync serve -addr :8080            start one replica (empty store)
//	merklesync sync  -local URL -peer URL   sync two running replicas
//	merklesync demo                          run the built-in acceptance demo
//
// All data is in-memory; there is intentionally no persistence or UI.
package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"log"
	"net"
	"net/http"
	"os"
	"time"

	"merklesync/internal/store"
	"merklesync/internal/syncapi"
)

func startHTTPServer(st *store.Store) (string, *http.Server) {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		log.Fatalf("listen: %v", err)
	}
	srv := &http.Server{Handler: syncapi.NewServer(st).Handler()}
	go func() { _ = srv.Serve(ln) }()
	return "http://" + ln.Addr().String(), srv
}

func main() {
	if len(os.Args) < 2 {
		usage()
		os.Exit(2)
	}
	switch os.Args[1] {
	case "serve":
		cmdServe(os.Args[2:])
	case "sync":
		cmdSync(os.Args[2:])
	case "demo":
		cmdDemo(os.Args[2:])
	case "-h", "--help", "help":
		usage()
	default:
		fmt.Fprintf(os.Stderr, "unknown subcommand %q\n\n", os.Args[1])
		usage()
		os.Exit(2)
	}
}

func usage() {
	fmt.Fprint(os.Stderr, `merklesync — anti-entropy Merkle-tree synchronization

usage:
  merklesync serve -addr :8080
  merklesync sync  -local http://127.0.0.1:8080 -peer http://127.0.0.1:9090
  merklesync demo [-seed 200]
`)
}

func cmdServe(args []string) {
	fs := flag.NewFlagSet("serve", flag.ExitOnError)
	addr := fs.String("addr", ":8080", "listen address")
	_ = fs.Parse(args)

	st := store.New()
	srv := &http.Server{
		Addr:              *addr,
		Handler:           syncapi.NewServer(st).Handler(),
		ReadHeaderTimeout: 5 * time.Second,
	}
	log.Printf("merklesync replica listening on %s (in-memory store)", *addr)
	if err := srv.ListenAndServe(); err != nil {
		log.Fatal(err)
	}
}

func cmdSync(args []string) {
	fs := flag.NewFlagSet("sync", flag.ExitOnError)
	local := fs.String("local", "", "URL of the local replica (its /apply is written)")
	peer := fs.String("peer", "", "URL of the peer replica")
	timeout := fs.Duration("timeout", 30*time.Second, "overall timeout")
	_ = fs.Parse(args)
	if *local == "" || *peer == "" {
		fs.Usage()
		os.Exit(2)
	}

	ctx, cancel := context.WithTimeout(context.Background(), *timeout)
	defer cancel()

	localClient := syncapi.NewClient(*local)
	peerClient := syncapi.NewClient(*peer)
	hl := syncapi.NewHTTPLocal(ctx, localClient)
	if err := hl.Refresh(); err != nil {
		log.Fatalf("fetch local snapshot: %v", err)
	}
	res, err := syncapi.NewSyncer(hl, peerClient).Sync(ctx)
	if err != nil {
		log.Fatalf("sync failed: %v", err)
	}
	out := struct {
		*syncapi.SyncStats
		Wire syncapi.Stats `json:"wire"`
	}{SyncStats: res, Wire: peerClient.Stats()}
	raw, _ := json.MarshalIndent(out, "", "  ")
	fmt.Println(string(raw))
}
