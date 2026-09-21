package integration

import (
	"context"
	"fmt"
	"net"
	"os"
	"os/exec"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/clearsettle/clearsettle/internal/domain"
	"github.com/clearsettle/clearsettle/internal/service"
	"github.com/clearsettle/clearsettle/internal/store"
)

// startPostgres starts an ephemeral postgres container (or uses
// CLEARSETTLE_TEST_DATABASE_URL) and returns its connection URL plus a
// cleanup func. The schema is migrated before return.
func startPostgres(t *testing.T) (string, func()) {
	t.Helper()
	if url := os.Getenv("CLEARSETTLE_TEST_DATABASE_URL"); url != "" {
		pool, err := pgxpool.New(context.Background(), url)
		if err != nil {
			t.Fatalf("connect test db: %v", err)
		}
		if _, err := pool.Exec(context.Background(),
			"DROP SCHEMA public CASCADE; CREATE SCHEMA public;"); err != nil {
			t.Fatalf("reset test db: %v", err)
		}
		pool.Close()
		if err := store.Migrate(context.Background(), mustPool(t, url)); err != nil {
			t.Fatalf("migrate: %v", err)
		}
		return url, func() {}
	}

	name := "clearsettle-test-" + strings.ToLower(randomSuffix())
	// Ask the kernel for a free host port, avoiding fixed-port collisions.
	ln, lerr := net.Listen("tcp", "127.0.0.1:0")
	if lerr != nil {
		t.Skipf("cannot allocate a free port: %v", lerr)
	}
	port := strconv.Itoa(ln.Addr().(*net.TCPAddr).Port)
	_ = ln.Close()
	cmd := exec.Command("docker", "run", "-d", "--rm",
		"--name", name,
		"-e", "POSTGRES_USER=clearsettle",
		"-e", "POSTGRES_PASSWORD=clearsettle",
		"-e", "POSTGRES_DB=clearsettle",
		"-p", "127.0.0.1:"+port+":5432",
		"postgres:16-alpine")
	out, err := cmd.CombinedOutput()
	if err != nil {
		t.Skipf("docker unavailable, skipping integration test: %v\n%s", err, out)
	}

	cleanup := func() {
		if os.Getenv("KEEP_DB") == "1" {
			t.Logf("keeping container %s", name)
			return
		}
		_ = exec.Command("docker", "stop", name).Run()
	}
	url := "postgres://clearsettle:clearsettle@127.0.0.1:" + port + "/clearsettle?sslmode=disable"

	// Wait for readiness.
	deadline := time.Now().Add(60 * time.Second)
	for time.Now().Before(deadline) {
		pool, err := pgxpool.New(context.Background(), url)
		if err == nil {
			if err := pool.Ping(context.Background()); err == nil {
				pool.Close()
				break
			}
			pool.Close()
		}
		time.Sleep(500 * time.Millisecond)
	}
	if err := store.Migrate(context.Background(), mustPool(t, url)); err != nil {
		cleanup()
		t.Fatalf("migrate: %v", err)
	}
	return url, cleanup
}

func mustPool(t *testing.T, url string) *pgxpool.Pool {
	t.Helper()
	pool, err := store.Connect(context.Background(), url)
	if err != nil {
		t.Fatalf("pool: %v", err)
	}
	return pool
}

func randomSuffix() string {
	return fmt.Sprintf("%x", time.Now().UnixNano())
}

// testEnv bundles a service with pinned time and ready actors.
type testEnv struct {
	svc      *service.Service
	admin    service.Actor
	adminKey string
	clock    *domain.FixedClock
}

func newTestEnv(t *testing.T, url string) *testEnv {
	t.Helper()
	pool := mustPool(t, url)
	clock := &domain.FixedClock{T: time.Date(2026, 1, 15, 10, 0, 0, 0, time.UTC)}
	svc := service.New(pool, clock)

	const adminKey = "sk_ad_testadmin0000000000000000000000"
	if _, err := svc.BootstrapAdmin(context.Background(), adminKey); err != nil {
		t.Fatalf("bootstrap admin: %v", err)
	}
	admin, err := svc.Authenticate(context.Background(), adminKey)
	if err != nil {
		t.Fatalf("auth admin: %v", err)
	}
	return &testEnv{svc: svc, admin: admin, adminKey: adminKey, clock: clock}
}
