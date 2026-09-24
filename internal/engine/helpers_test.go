package engine_test

import (
	"context"
	"os"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"

	"resumable-bt/internal/engine"
	"resumable-bt/internal/store"
	"resumable-bt/internal/stub"
)

const defaultDSN = "postgres://btapp:btapp_dev_pw@localhost:5432/btdb?sslmode=disable"

func dsn() string {
	if v := os.Getenv("BT_TEST_DSN"); v != "" {
		return v
	}
	return defaultDSN
}

// newTestStore opens the pool and migrates the schema.
func newTestStore(t *testing.T) *store.Store {
	t.Helper()
	ctx := context.Background()
	st, err := store.Open(ctx, dsn())
	if err != nil {
		t.Skipf("postgresql not available at %s (%v); set BT_TEST_DSN to run", dsn(), err)
	}
	if err := st.Migrate(ctx); err != nil {
		t.Fatalf("migrate: %v", err)
	}
	t.Cleanup(st.Close)
	return st
}

func wipe(t *testing.T, st *store.Store) {
	t.Helper()
	err := st.Tx(context.Background(), func(tx pgx.Tx) error {
		_, err := tx.Exec(context.Background(),
			`TRUNCATE invocations, node_states, ticks, executions,
			 tree_versions, trees RESTART IDENTITY CASCADE`)
		return err
	})
	if err != nil {
		t.Fatalf("truncate: %v", err)
	}
}

// newEngine builds an engine with a fresh registry against an empty database.
func newEngine(t *testing.T) (*engine.Engine, *store.Store, *stub.Registry) {
	t.Helper()
	st := newTestStore(t)
	wipe(t, st)
	reg := stub.NewRegistry()
	eng := engine.New(st, reg)
	t.Cleanup(eng.Close)
	return eng, st, reg
}

// publishJSON publishes a tree and returns its version.
func publishJSON(t *testing.T, eng *engine.Engine, name, raw string) int64 {
	t.Helper()
	v, _, err := eng.Publish(context.Background(), name, []byte(raw))
	if err != nil {
		t.Fatalf("publish %s: %v", name, err)
	}
	return v.Version
}

func startExec(t *testing.T, eng *engine.Engine, name string) uuid.UUID {
	t.Helper()
	id, _, err := eng.StartExecution(context.Background(), name, 0)
	if err != nil {
		t.Fatalf("start: %v", err)
	}
	return id
}

func tickOnce(t *testing.T, eng *engine.Engine, id uuid.UUID, note string) engine.TickResult {
	t.Helper()
	res, err := eng.Tick(context.Background(), id, note)
	if err != nil {
		t.Fatalf("tick: %v", err)
	}
	return res
}

// waitForStatus polls until the execution reaches one of want.
func waitForStatus(t *testing.T, eng *engine.Engine, id uuid.UUID, want ...string) store.ExecutionRow {
	t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		snap, err := eng.Snapshot(context.Background(), id)
		if err != nil {
			t.Fatalf("snapshot: %v", err)
		}
		for _, w := range want {
			if snap.Execution.Status == w {
				return snap.Execution
			}
		}
		time.Sleep(10 * time.Millisecond)
	}
	snap, _ := eng.Snapshot(context.Background(), id)
	t.Fatalf("execution %s did not reach %v; status=%s", id, want, snap.Execution.Status)
	return snap.Execution
}

// invocationByNode fetches the single invocation of a node (tests use unique
// node ids per execution).
func invocationByNode(t *testing.T, st *store.Store, id uuid.UUID, node string) store.InvocationRow {
	t.Helper()
	rows, err := st.ListInvocations(context.Background(), id)
	if err != nil {
		t.Fatalf("list invocations: %v", err)
	}
	for _, r := range rows {
		if r.NodeID == node {
			return r
		}
	}
	t.Fatalf("no invocation for node %q", node)
	return store.InvocationRow{}
}

// waitInvocation blocks until the node's invocation reaches want.
func waitInvocation(t *testing.T, st *store.Store, id uuid.UUID, node, want string) store.InvocationRow {
	t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		if r, err := st.ListInvocations(context.Background(), id); err == nil {
			for _, row := range r {
				if row.NodeID == node && row.Status == want {
					return row
				}
			}
		}
		time.Sleep(5 * time.Millisecond)
	}
	got := invocationByNode(t, st, id, node)
	t.Fatalf("invocation %s of %s never reached %s (last=%s)", node, id, want, got.Status)
	return got
}
