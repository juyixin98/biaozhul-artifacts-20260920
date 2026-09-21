// Command sitevitals runs the API server, the worker pool and (optionally) the
// built-in demo test site from one process.
package main

import (
	"context"
	"errors"
	"flag"
	"log"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"gorm.io/gorm"

	"sitevitals/internal/api"
	"sitevitals/internal/budget"
	"sitevitals/internal/config"
	"sitevitals/internal/migrate"
	"sitevitals/internal/models"
	"sitevitals/internal/store"
	"sitevitals/internal/testsite"
	"sitevitals/internal/worker"
)

func main() {
	if len(os.Args) < 2 {
		usage()
		os.Exit(2)
	}
	cfg := config.Load()
	switch os.Args[1] {
	case "serve":
		fs := flag.NewFlagSet("serve", flag.ExitOnError)
		migrateOnly := fs.Bool("migrate-only", false, "run migrations and exit")
		noMigrate := fs.Bool("no-migrate", false, "skip automatic migration on startup")
		enableTestSite := fs.Bool("testsite", false, "also start the built-in demo test site")
		testSiteAddr := fs.String("testsite-addr", cfg.TestSiteAddr, "demo test site bind address")
		seed := fs.Bool("seed", true, "seed the demo site allow-list when --testsite is on")
		_ = fs.Parse(os.Args[2:])

		db, err := store.Open(cfg.MySQLDSN)
		if err != nil {
			log.Fatalf("database: %v", err)
		}
		if !*noMigrate {
			ran, err := migrate.Up(db)
			if err != nil {
				log.Fatalf("migration: %v", err)
			}
			if len(ran) > 0 {
				log.Printf("applied migrations: %v", ran)
			}
		}
		if *migrateOnly {
			log.Print("migration complete; exiting")
			return
		}

		rootCtx, stop := signalContext()
		defer stop()

		var ts *testsite.Server
		if *enableTestSite {
			ts = testsite.New(*testSiteAddr)
			if err := ts.Start(); err != nil {
				log.Fatalf("test site: %v", err)
			}
			log.Printf("demo test site listening on %s", *testSiteAddr)
			if *seed {
				if err := seedDemo(db, *testSiteAddr); err != nil {
					log.Printf("demo seed warning: %v", err)
				}
			}
		}

		repo := store.NewRepo(db)
		queue := store.NewQueue(db)
		evaluator := budget.NewEvaluator(repo)
		pool := worker.NewPool(cfg, queue, repo, evaluator, nil)
		go pool.Run(rootCtx)

		srv := &http.Server{
			Addr:              cfg.HTTPAddr,
			Handler:           api.NewServer(repo, queue).Router(),
			ReadHeaderTimeout: 10 * time.Second,
		}
		go func() {
			log.Printf("API listening on %s (workers=%d, max_browsers=%d)",
				cfg.HTTPAddr, cfg.Workers, cfg.MaxBrowsers)
			if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
				log.Fatalf("http server: %v", err)
			}
		}()

		<-rootCtx.Done()
		log.Print("shutting down…")
		shCtx, cancel := context.WithTimeout(context.Background(), 8*time.Second)
		defer cancel()
		_ = srv.Shutdown(shCtx)
		if ts != nil {
			_ = ts.Shutdown(shCtx)
		}

	case "migrate":
		db, err := store.Open(cfg.MySQLDSN)
		if err != nil {
			log.Fatalf("database: %v", err)
		}
		ran, err := migrate.Up(db)
		if err != nil {
			log.Fatalf("migration: %v", err)
		}
		log.Printf("migrations applied: %v (total %d)", ran, len(ran))

	default:
		usage()
		os.Exit(2)
	}
}

func usage() {
	log.Print("usage: sitevitals <serve|migrate> [flags]")
}

func signalContext() (context.Context, func()) {
	ctx, cancel := context.WithCancel(context.Background())
	ch := make(chan os.Signal, 1)
	signal.Notify(ch, syscall.SIGINT, syscall.SIGTERM)
	go func() {
		<-ch
		cancel()
	}()
	return ctx, cancel
}

// seedDemo registers the local demo site and a catch-all allow rule for it.
// It is idempotent.
func seedDemo(db *gorm.DB, addr string) error {
	origin := "http://127.0.0.1" + normalizePort(addr)
	var site models.Site
	err := db.Where("scheme_host = ?", origin).Take(&site).Error
	if errors.Is(err, gorm.ErrRecordNotFound) {
		site = models.Site{
			Name:        "Local Demo Test Site",
			SchemeHost:  origin,
			Description: "built-in demo server; whole origin allow-listed",
			Enabled:     true,
		}
		if err := db.Create(&site).Error; err != nil {
			return err
		}
	} else if err != nil {
		return err
	} else if !site.Enabled {
		if err := db.Model(&site).Update("enabled", true).Error; err != nil {
			return err
		}
	}

	var count int64
	if err := db.Model(&models.AllowedURL{}).
		Where("site_id = ? AND url_pattern = ?", site.ID, origin+"/*").Count(&count).Error; err != nil {
		return err
	}
	if count == 0 {
		if err := db.Create(&models.AllowedURL{
			SiteID:     site.ID,
			URLPattern: origin + "/*",
			Note:       "demo: allow every path on the test site origin",
			Enabled:    true,
		}).Error; err != nil {
			return err
		}
		log.Printf("seeded demo site %s with allow rule %s/*", origin, origin)
	}
	return nil
}

func normalizePort(addr string) string {
	// addr like ":8093" -> ":8093"; "127.0.0.1:8093" -> ":8093".
	for i := len(addr) - 1; i >= 0; i-- {
		if addr[i] == ':' {
			return addr[i:]
		}
	}
	return addr
}
