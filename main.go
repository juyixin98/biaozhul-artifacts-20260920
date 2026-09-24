// Command robotdispatch serves the offline multi-robot task allocation
// API (net/http only, no third-party dependencies).
//
// Usage:
//
//	go run .                       # start HTTP server on :8080
//	go run . -addr :9090          # custom listen address
//	go run . -file instance.json  # one-shot: solve a JSON file and print
package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"robotdispatch/api"
	"robotdispatch/model"
	"robotdispatch/solver"
)

func main() {
	addr := flag.String("addr", ":8080", "HTTP listen address")
	file := flag.String("file", "", "solve the given JSON instance once and print the result, then exit")
	flag.Parse()

	if *file != "" {
		os.Exit(solveFile(*file))
	}

	srv := &http.Server{
		Addr:              *addr,
		Handler:           api.NewServer(),
		ReadHeaderTimeout: 10 * time.Second,
		ReadTimeout:       30 * time.Second,
		WriteTimeout:      30 * time.Second,
		IdleTimeout:       60 * time.Second,
	}

	stop := make(chan os.Signal, 1)
	signal.Notify(stop, os.Interrupt, syscall.SIGTERM)

	go func() {
		log.Printf("robot-task-allocation listening on %s", *addr)
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Fatalf("server error: %v", err)
		}
	}()

	<-stop
	log.Printf("shutting down")
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := srv.Shutdown(ctx); err != nil {
		log.Printf("graceful shutdown failed: %v", err)
	}
}

func solveFile(path string) int {
	f, err := os.Open(path)
	if err != nil {
		fmt.Fprintf(os.Stderr, "open %s: %v\n", path, err)
		return 2
	}
	defer f.Close()

	data, err := io.ReadAll(io.LimitReader(f, 1<<20))
	if err != nil {
		fmt.Fprintf(os.Stderr, "read %s: %v\n", path, err)
		return 2
	}
	var req model.Request
	if err := json.Unmarshal(data, &req); err != nil {
		fmt.Fprintf(os.Stderr, "parse %s: %v\n", path, err)
		return 2
	}
	epd, err := req.Validate()
	if err != nil {
		fmt.Fprintf(os.Stderr, "invalid instance: %v\n", err)
		return 2
	}
	resp := solver.Solve(&req, epd)
	out, err := json.MarshalIndent(resp, "", "  ")
	if err != nil {
		fmt.Fprintf(os.Stderr, "encode result: %v\n", err)
		return 2
	}
	fmt.Println(string(out))
	return 0
}
