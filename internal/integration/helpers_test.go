package integration

import (
	"context"
	"os"
	"strings"
	"testing"
	"time"

	"forkindexer/internal/domain"
	"forkindexer/internal/pgstore"
)

const (
	addrA = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
	addrB = "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
	addrC = "0xcccccccccccccccccccccccccccccccccccccccc"
	addrD = "0xdddddddddddddddddddddddddddddddddddddddd"
)

// testDSN reads FORKINDEXER_TEST_DSN then falls back to the local dev DSN.
func testDSN() string {
	if v := os.Getenv("FORKINDEXER_TEST_DSN"); v != "" {
		return v
	}
	return "postgres:///forkindexer_test?host=/var/run/postgresql&sslmode=disable"
}

// newTestStore opens a connection to a per-test schema and migrates it.
// Schemas give full isolation and let parallel tests share one database.
func newTestStore(t *testing.T) *pgstore.Store {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	schema := "t_" + strings.ReplaceAll(t.Name(), "/", "_")
	schema = strings.Map(func(r rune) rune {
		if r >= 'a' && r <= 'z' || r >= '0' && r <= '9' || r == '_' {
			return r
		}
		return '_'
	}, schema)

	// Bootstrap the per-test schema over a maintenance connection.
	boot, err := pgstore.New(ctx, testDSN())
	if err != nil {
		t.Skipf("postgresql not available (%v); set FORKINDEXER_TEST_DSN to run integration tests", err)
	}
	if _, err := boot.DB().ExecContext(ctx, "DROP SCHEMA IF EXISTS "+schema+" CASCADE"); err != nil {
		boot.Close()
		t.Fatalf("drop schema: %v", err)
	}
	if _, err := boot.DB().ExecContext(ctx, "CREATE SCHEMA "+schema); err != nil {
		boot.Close()
		t.Fatalf("create schema: %v", err)
	}
	boot.Close()

	// pgx applies search_path for every pooled connection, so REPEATABLE READ
	// read transactions and the writer all see the per-test schema.
	dsn := testDSN()
	if strings.Contains(dsn, "?") {
		dsn += "&search_path=" + schema
	} else {
		dsn += "?search_path=" + schema
	}
	s, err := pgstore.New(ctx, dsn)
	if err != nil {
		t.Skipf("postgresql not available (%v); set FORKINDEXER_TEST_DSN to run integration tests", err)
	}
	if err := s.Migrate(ctx); err != nil {
		t.Fatalf("migrate: %v", err)
	}
	t.Cleanup(func() {
		ctx2, cancel2 := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel2()
		cleanup, err := pgstore.New(ctx2, testDSN())
		if err == nil {
			_, _ = cleanup.DB().ExecContext(ctx2, "DROP SCHEMA IF EXISTS "+schema+" CASCADE")
			cleanup.Close()
		}
		s.Close()
	})
	return s
}

type chainBuilder struct {
	t  *testing.T
	mu map[string]*domain.Block
}

func newChainBuilder(t *testing.T) *chainBuilder {
	return &chainBuilder{t: t, mu: map[string]*domain.Block{}}
}

func (c *chainBuilder) block(parent string, height int64, ts []domain.Transfer) *domain.Block {
	raw, err := domain.CanonicalBody(parent, height, ts)
	if err != nil {
		c.t.Fatalf("canonical body: %v", err)
	}
	b := &domain.Block{
		Hash:       domain.BlockHash(raw),
		ParentHash: parent,
		Height:     height,
		Transfers:  ts,
		Raw:        raw,
	}
	if err := domain.Normalize(b); err != nil {
		c.t.Fatalf("normalize: %v", err)
	}
	c.mu[b.Hash] = b
	return b
}

// deliver inserts with the given sequence; seq 0 means bare (no cursor).
func deliver(t *testing.T, s *pgstore.Store, b *domain.Block, seq int64) pgstore.IngestResult {
	t.Helper()
	var p *int64
	if seq > 0 {
		p = &seq
	}
	res, err := s.IngestEnvelope(context.Background(), b, p)
	if err != nil {
		t.Fatalf("ingest %s (h=%d seq=%d): %v", b.Hash[:10], b.Height, seq, err)
	}
	return res
}

func deliverErr(t *testing.T, s *pgstore.Store, b *domain.Block, seq int64) error {
	t.Helper()
	var p *int64
	if seq > 0 {
		p = &seq
	}
	_, err := s.IngestEnvelope(context.Background(), b, p)
	return err
}

func mustVerifyOK(t *testing.T, s *pgstore.Store) {
	t.Helper()
	rep, err := s.VerifyFromGenesis(context.Background())
	if err != nil {
		t.Fatalf("verify: %v", err)
	}
	if !rep.OK {
		t.Fatalf("verification diverged:\n mismatches=%v\n chain=%v\n notes=%v",
			rep.BalanceMismatch, rep.ChainMismatch, rep.Notes)
	}
}

// buildThreeForkScenario returns the seven named blocks from the README.
type scenario struct {
	G, A1, A2, B1, B2, B3, C2 *domain.Block
}

func (c *chainBuilder) threeForks() scenario {
	G := c.block(domain.ZeroHash, 0, []domain.Transfer{{From: addrA, To: addrB, Amount: 1000}})
	A1 := c.block(G.Hash, 1, []domain.Transfer{{From: addrB, To: addrC, Amount: 100}})
	B1 := c.block(G.Hash, 1, []domain.Transfer{{From: addrB, To: addrD, Amount: 40}})
	A2 := c.block(A1.Hash, 2, []domain.Transfer{{From: addrC, To: addrA, Amount: 10}})
	B2 := c.block(B1.Hash, 2, []domain.Transfer{{From: addrD, To: addrA, Amount: 5}})
	C2 := c.block(B1.Hash, 2, []domain.Transfer{{From: addrB, To: addrC, Amount: 1}})
	B3 := c.block(B2.Hash, 3, []domain.Transfer{{From: addrA, To: addrB, Amount: 200}})
	return scenario{G, A1, A2, B1, B2, B3, C2}
}

func balancesOf(t *testing.T, s *pgstore.Store, addrs ...string) map[string]int64 {
	t.Helper()
	out := map[string]int64{}
	for _, a := range addrs {
		v, _, err := s.Balance(context.Background(), a)
		if err != nil {
			t.Fatal(err)
		}
		out[a] = v
	}
	return out
}
