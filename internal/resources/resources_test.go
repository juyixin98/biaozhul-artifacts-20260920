package resources

import (
	"context"
	"errors"
	"fmt"
	"testing"
	"time"
)

type stubResource struct {
	name string
	err  error
}

func (s stubResource) Name() string                    { return s.name }
func (s stubResource) Close(ctx context.Context) error { return s.err }

func TestCloseAllLIFOOrder(t *testing.T) {
	r := NewRegistry()
	r.Register(stubResource{name: "a"})
	r.Register(stubResource{name: "b"})
	r.Register(stubResource{name: "c"})

	now := time.Unix(10, 0)
	rec := r.CloseAll(context.Background(), func() time.Time { return now })
	if len(rec) != 3 {
		t.Fatalf("records = %d, want 3", len(rec))
	}
	want := []string{"c", "b", "a"}
	for i, w := range want {
		if rec[i].Name != w || rec[i].Order != i+1 || !rec[i].ClosedAt.Equal(now) {
			t.Errorf("rec[%d] = %+v, want name=%s order=%d", i, rec[i], w, i+1)
		}
	}
}

func TestCloseAllIdempotentAndErrorRecorded(t *testing.T) {
	r := NewRegistry()
	boom := errors.New("boom")
	r.Register(stubResource{name: "ok"})
	r.Register(stubResource{name: "bad", err: boom})

	rec := r.CloseAll(context.Background(), time.Now)
	if rec[0].Name != "bad" || rec[0].Err != fmt.Sprint(boom) {
		t.Errorf("error not recorded: %+v", rec[0])
	}
	rec2 := r.CloseAll(context.Background(), time.Now)
	if len(rec2) != 2 {
		t.Fatalf("second CloseAll must be a no-op, got %d records", len(rec2))
	}
}
