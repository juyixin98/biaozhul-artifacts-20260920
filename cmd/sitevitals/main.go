package main

import (
	"context"
	"errors"
	"log"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"sitevitals/internal/api"
	"sitevitals/internal/browser"
	"sitevitals/internal/config"
	"sitevitals/internal/demo"
	"sitevitals/internal/store"
	"sitevitals/internal/whitelist"
	"sitevitals/internal/worker"
)

func main() {
	if len(os.Args) < 2 {
		usage()
		os.Exit(2)
	}
	cmd := os.Args[1]
	args := os.Args[2:]
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	switch cmd {
	case "serve":
		if err := runServe(ctx); err != nil {
			log.Fatalf("serve: %v", err)
		}
	case "migrate":
		if err := runMigrate(ctx); err != nil {
			log.Fatalf("migrate: %v", err)
		}
	case "demo-seed":
		demoOrigin := getFlag(args, "--demo-origin", "http://demo:8090")
		if err := runDemoSeed(ctx, demoOrigin); err != nil {
			log.Fatalf("demo-seed: %v", err)
		}
	case "help", "-h", "--help":
		usage()
	default:
		usage()
		os.Exit(2)
	}
}

func usage() {
	log.Print(`sitevitals — local intranet web-vitals collector

usage: sitevitals <command> [flags]

commands:
  serve       run API + worker (+ optional demo site); default command
  migrate     apply database migrations and exit
  demo-seed   register the local demo site in the whitelist
                [--demo-origin http://demo:8090]
  help        show this message

environment: see README.md (MYSQL_*, HTTP_ADDR, CHROME_PATH,
BROWSER_CONCURRENCY, LEASE_DURATION, ENABLE_DEMO, ...)`)
}

func getFlag(args []string, name, def string) string {
	for i, a := range args {
		if a == name && i+1 < len(args) {
			return args[i+1]
		}
	}
	return def
}

func runMigrate(ctx context.Context) error {
	cfg := config.Load()
	if err := store.EnsureDatabase(cfg.MySQLDSN); err != nil {
		return err
	}
	db, err := store.Open(ctx, cfg.MySQLDSN)
	if err != nil {
		return err
	}
	st := store.New(db)
	if err := st.AutoMigrate(ctx); err != nil {
		return err
	}
	return st.Seed(ctx)
}

func runDemoSeed(ctx context.Context, origin string) error {
	cfg := config.Load()
	if err := store.EnsureDatabase(cfg.MySQLDSN); err != nil {
		return err
	}
	db, err := store.Open(ctx, cfg.MySQLDSN)
	if err != nil {
		return err
	}
	st := store.New(db)
	if err := st.AutoMigrate(ctx); err != nil {
		return err
	}
	if _, err := st.EnsureSite(ctx, "local-demo", origin, "/", true); err != nil {
		return err
	}
	log.Printf("demo site whitelisted: %s", origin)
	return nil
}

func runServe(ctx context.Context) error {
	cfg := config.Load()
	log.Printf("sitevitals starting (worker=%v api=%v demo=%v concurrency=%d)",
		cfg.EnableWorker, cfg.EnableAPI, cfg.EnableDemo, cfg.BrowserConcurrency)

	db, err := store.Open(ctx, cfg.MySQLDSN)
	if err != nil {
		return err
	}
	st := store.New(db)
	if cfg.AutoMigrate {
		if err := st.AutoMigrate(ctx); err != nil {
			return err
		}
		if err := st.Seed(ctx); err != nil {
			return err
		}
	}

	// Matcher is rebuilt on each request to sites so whitelist edits take
	// effect without a restart; cheap for the expected small entry count.
	matcher := func() *whitelist.Matcher {
		sites, err := st.ListSites(ctx, false)
		if err != nil {
			log.Printf("load sites for matcher: %v", err)
			return whitelist.NewMatcher(nil)
		}
		return whitelist.NewMatcher(sites)
	}

	var srv *http.Server
	if cfg.EnableAPI {
		router := api.NewServer(st, matcher).Router()
		srv = &http.Server{Addr: cfg.HTTPAddr, Handler: router}
		go func() {
			log.Printf("API listening on %s", cfg.HTTPAddr)
			if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
				log.Fatalf("http server: %v", err)
			}
		}()
	}

	var demoSrv *http.Server
	if cfg.EnableDemo {
		demoSrv = &http.Server{Addr: cfg.DemoAddr, Handler: demo.NewServer()}
		go func() {
			log.Printf("demo site listening on %s", cfg.DemoAddr)
			if err := demoSrv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
				log.Printf("demo server: %v", err)
			}
		}()
	}

	if cfg.EnableWorker {
		n := cfg.BrowserConcurrency
		if n < 1 {
			n = 1
		}
		insts := make([]*browser.Instance, 0, n)
		for i := 0; i < n; i++ {
			inst := browser.NewInstance(browser.Options{
				ChromePath: cfg.ChromePath, Headless: cfg.Headless, WSURL: cfg.ChromiumWSURL,
			})
			if err := inst.Start(ctx); err != nil {
				return err
			}
			insts = append(insts, inst)
			log.Printf("chromium instance %d ready (headless=%v)", i, cfg.Headless)
		}
		pool := worker.New(cfg, st, insts)
		go func() {
			if err := pool.Run(ctx); err != nil {
				log.Printf("worker pool: %v", err)
			}
		}()
		defer func() {
			for _, in := range insts {
				_ = in.Close()
			}
		}()
	}

	<-ctx.Done()
	log.Print("shutting down ...")
	shutdownCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if srv != nil {
		_ = srv.Shutdown(shutdownCtx)
	}
	if demoSrv != nil {
		_ = demoSrv.Shutdown(shutdownCtx)
	}
	time.Sleep(200 * time.Millisecond)
	return nil
}
