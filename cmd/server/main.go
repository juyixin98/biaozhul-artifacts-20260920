package main

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"log"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"syscall"
	"time"

	"github.com/jmoiron/sqlx"
	_ "github.com/lib/pq"

	"domainengine/internal/clock"
	"domainengine/internal/httpapi"
	"domainengine/internal/jobs"
	"domainengine/internal/migrate"
	"domainengine/internal/service"
)

type config struct {
	port               string
	databaseURL        string
	authKey            []byte
	ephemeralKey       bool
	enableClockControl bool
	maintenanceEvery   time.Duration
	svc                service.Config
}

func getenv(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

func envInt(key string, def int) int {
	if v := os.Getenv(key); v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			return n
		}
	}
	return def
}

func loadConfig() config {
	cfg := config{
		port:               getenv("PORT", "8080"),
		databaseURL:        getenv("DATABASE_URL", "postgres://postgres:postgres@localhost:5432/domains?sslmode=disable"),
		enableClockControl: getenv("ENABLE_CLOCK_CONTROL", "false") == "true",
		maintenanceEvery:   time.Duration(envInt("MAINTENANCE_INTERVAL_SECONDS", 30)) * time.Second,
		svc: service.Config{
			ExpiredGraceDays:            envInt("EXPIRED_GRACE_DAYS", 30),
			RedemptionDays:              envInt("REDEMPTION_DAYS", 30),
			PendingDeleteDays:           envInt("PENDING_DELETE_DAYS", 5),
			TransferWaitDays:            envInt("TRANSFER_WAIT_DAYS", 5),
			TransferApprovalTimeoutDays: envInt("TRANSFER_APPROVAL_TIMEOUT_DAYS", 7),
		},
	}
	if hexKey := os.Getenv("AUTH_CODE_KEY"); hexKey != "" {
		key, err := hex.DecodeString(hexKey)
		if err != nil || len(key) != 32 {
			log.Fatal("AUTH_CODE_KEY must be 64 hex chars (32 bytes)")
		}
		cfg.authKey = key
	} else {
		// Dev convenience: ephemeral key. Auth codes encrypted with it become
		// undecryptable after restart, so set AUTH_CODE_KEY for anything real.
		cfg.authKey = make([]byte, 32)
		if _, err := rand.Read(cfg.authKey); err != nil {
			log.Fatal(err)
		}
		cfg.ephemeralKey = true
	}
	return cfg
}

func main() {
	logger := log.New(os.Stdout, "domainengine ", log.LstdFlags)
	cfg := loadConfig()
	if cfg.ephemeralKey {
		logger.Println("WARNING: AUTH_CODE_KEY not set; using an ephemeral key (auth codes will not survive restart)")
	}

	var db *sqlx.DB
	var err error
	for i := 0; i < 30; i++ {
		db, err = sqlx.Connect("postgres", cfg.databaseURL)
		if err == nil {
			break
		}
		logger.Printf("waiting for database: %v", err)
		time.Sleep(time.Second)
	}
	if err != nil {
		logger.Fatalf("cannot connect to database: %v", err)
	}
	defer db.Close()

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	if err := migrate.Up(ctx, db); err != nil {
		logger.Fatalf("migrations: %v", err)
	}
	logger.Println("migrations applied")

	var clk clock.Clock = clock.Real{}
	var offset *clock.Offset
	if cfg.enableClockControl {
		offset = clock.NewOffset()
		clk = offset
		logger.Println("simulated clock control enabled (POST /api/admin/clock/advance)")
	}

	svc := service.New(db, clk, cfg.svc, cfg.authKey)
	go jobs.NewRunner(svc, cfg.maintenanceEvery, logger).Run(ctx)

	e := httpapi.NewRouter(svc, db, offset)
	go func() {
		if err := e.Start(":" + cfg.port); err != nil && err != http.ErrServerClosed {
			logger.Fatalf("server: %v", err)
		}
	}()
	logger.Printf("listening on :%s", cfg.port)

	<-ctx.Done()
	shutdownCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	_ = e.Shutdown(shutdownCtx)
	logger.Println("stopped")
}
