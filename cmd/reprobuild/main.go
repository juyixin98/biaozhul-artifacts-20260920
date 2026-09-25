// Command reprobuild is the reproducible archive build service.
//
// Subcommands:
//
//	reprobuild serve [-addr :8080] [-state-dir .reprobuild]
//	reprobuild pack  -src ./tree -o artifact.tar
//
// The service is local-only by default and never connects to any cloud
// platform. See README.md for the API reference.
package main

import (
	"context"
	"flag"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"syscall"
	"time"

	"reprobuild/internal/api"
	"reprobuild/internal/archive"
	"reprobuild/internal/builder"
)

func main() {
	logger := slog.New(slog.NewTextHandler(os.Stderr, &slog.HandlerOptions{Level: slog.LevelInfo}))
	if len(os.Args) < 2 {
		usage()
		os.Exit(2)
	}
	var err error
	switch os.Args[1] {
	case "serve":
		err = runServe(os.Args[2:], logger)
	case "pack":
		err = runPack(os.Args[2:])
	case "-h", "--help", "help":
		usage()
	default:
		usage()
		err = fmt.Errorf("unknown subcommand %q", os.Args[1])
	}
	if err != nil {
		fmt.Fprintln(os.Stderr, "error:", err)
		os.Exit(1)
	}
}

func usage() {
	fmt.Fprint(os.Stderr, `reprobuild - reproducible archive build service

Usage:
  reprobuild serve [-addr HOST:PORT] [-state-dir DIR] [-fixture-timeout D]
  reprobuild pack  -src DIR -o FILE [-fixed-time SECONDS]
`)
}

func runServe(args []string, logger *slog.Logger) error {
	fs := flag.NewFlagSet("serve", flag.ContinueOnError)
	addr := fs.String("addr", "127.0.0.1:8080", "listen address (local-only by default)")
	stateDir := fs.String("state-dir", ".reprobuild", "directory for work/, cache/ and data/")
	timeout := fs.Duration("fixture-timeout", 60*time.Second, "per-fixture execution timeout")
	if err := fs.Parse(args); err != nil {
		return err
	}
	abs, err := filepath.Abs(*stateDir)
	if err != nil {
		return err
	}
	svc, err := builder.New(builder.Config{
		WorkDir:          filepath.Join(abs, "work"),
		CacheDir:         filepath.Join(abs, "cache"),
		DataDir:          filepath.Join(abs, "data"),
		FixtureTimeout:   *timeout,
		MaxFixtureOutput: 64 * 1024,
	})
	if err != nil {
		return err
	}
	srv := &http.Server{
		Addr:              *addr,
		Handler:           api.NewServer(svc, logger).Routes(),
		ReadHeaderTimeout: 10 * time.Second,
	}
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	go func() {
		logger.Info("reprobuild listening (local-only, no cloud connections)", "addr", *addr, "state", abs)
		if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			logger.Error("server failed", "err", err)
			os.Exit(1)
		}
	}()
	<-ctx.Done()
	logger.Info("shutting down")
	shCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	return srv.Shutdown(shCtx)
}

func runPack(args []string) error {
	fs := flag.NewFlagSet("pack", flag.ContinueOnError)
	src := fs.String("src", "", "source directory to archive (required)")
	out := fs.String("o", "", "output tar path (required)")
	fixed := fs.Int64("fixed-time", 0, "fixed entry mtime as Unix seconds (default: 1970-01-01)")
	shuffle := fs.Int64("shuffle-seed", 0, "randomize readdir order with this seed (acceptance testing; output must stay identical)")
	if err := fs.Parse(args); err != nil {
		return err
	}
	if *src == "" || *out == "" {
		fs.Usage()
		return fmt.Errorf("-src and -o are required")
	}
	f, err := os.Create(*out)
	if err != nil {
		return err
	}
	opts := &archive.Options{}
	if *fixed != 0 {
		opts.FixedTime = time.Unix(*fixed, 0).UTC()
	}
	if *shuffle != 0 {
		opts.Lister = newShuffledLister(*shuffle)
	}
	n, sum, err := archive.WriteTarHashed(f, *src, opts)
	if err != nil {
		f.Close()
		return err
	}
	if err := f.Close(); err != nil {
		return err
	}
	fi, _ := os.Stat(*out)
	fmt.Printf("wrote %s: %d entries, %d bytes, sha256=%s\n", *out, n, fi.Size(), sum)
	return nil
}
