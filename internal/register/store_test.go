package register

import (
	"errors"
	"sync"
	"sync/atomic"
	"testing"
)

func TestReadInRangeAndOut(t *testing.T) {
	s := New(100)
	if vals, ok := s.Read(98, 2); !ok || len(vals) != 2 {
		t.Fatalf("boundary read failed: %v ok=%v", vals, ok)
	}
	if _, ok := s.Read(99, 2); ok {
		t.Fatal("read crossing boundary must fail")
	}
	if _, ok := s.Read(0, 0); ok {
		t.Fatal("zero-quantity read must fail")
	}
}

func TestWriteBatchSuccessAndRollback(t *testing.T) {
	s := New(10)

	var seen int64
	err := s.WriteBatch(2, []uint16{11, 12, 13}, func() error { atomic.AddInt64(&seen, 1); return nil })
	if err != nil {
		t.Fatal(err)
	}
	got, _ := s.Read(2, 3)
	if got[0] != 11 || got[1] != 12 || got[2] != 13 {
		t.Fatalf("values not applied: %v", got)
	}

	// Failing commit must roll back exactly the touched words.
	boom := errors.New("disk on fire")
	if err := s.WriteBatch(2, []uint16{99, 98, 97}, func() error { return boom }); !errors.Is(err, boom) {
		t.Fatalf("want boom, got %v", err)
	}
	got, _ = s.Read(2, 3)
	if got[0] != 11 || got[1] != 12 || got[2] != 13 {
		t.Fatalf("rollback failed: %v", got)
	}
}

func TestWriteBatchOutOfRangeRejectsAll(t *testing.T) {
	s := New(10)
	// Seed nearby valid registers.
	if err := s.WriteBatch(5, []uint16{5, 6, 7, 8}, nil); err != nil {
		t.Fatal(err)
	}
	// Batch starts valid but crosses the boundary: whole write must fail.
	called := false
	err := s.WriteBatch(8, []uint16{80, 81, 82}, func() error { called = true; return nil })
	if !errors.Is(err, ErrOutOfRange) {
		t.Fatalf("want ErrOutOfRange, got %v", err)
	}
	if called {
		t.Fatal("commit must not run when range validation fails")
	}
	got, _ := s.Read(5, 5)
	for i, want := range []uint16{5, 6, 7, 8, 0} {
		if got[i] != want {
			t.Fatalf("register %d = %d, want %d (partial write leaked!)", 5+i, got[i], want)
		}
	}

	// Batch starting outside the bank also fails wholesale.
	if err := s.WriteBatch(10, []uint16{1}, nil); !errors.Is(err, ErrOutOfRange) {
		t.Fatalf("start-at-boundary: want ErrOutOfRange got %v", err)
	}
}

// TestConcurrentReadersNeverSeeHalfBatch runs many writers (each writes a
// uniform batch) against many readers that only accept uniform snapshots.
// Any torn read fails the race.
func TestConcurrentReadersNeverSeeHalfBatch(t *testing.T) {
	const nReg = 64
	s := New(nReg)

	var wg sync.WaitGroup
	stop := make(chan struct{})
	var failures atomic.Int64

	// Writers: batch size 8 moving across the bank; all words in a batch
	// share one marker, so a half-applied batch is detectable.
	wg.Add(1)
	go func() {
		defer wg.Done()
		var marker uint16 = 1
		for i := 0; i < 4000; i++ {
			start := (i * 8) % (nReg - 8)
			values := make([]uint16, 8)
			for j := range values {
				values[j] = marker
			}
			if err := s.WriteBatch(uint16(start), values, nil); err != nil {
				t.Errorf("write: %v", err)
				return
			}
			marker++
		}
		close(stop)
	}()

	// Readers: overlapping 8-wide snapshots; within a snapshot the words
	// written by one batch are equal. Since batches at different positions
	// may legitimately differ, we check the invariant at a fixed position
	// pair instead: positions 0..7 are only ever written together.
	for r := 0; r < 6; r++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for {
				select {
				case <-stop:
					return
				default:
				}
				vals, ok := s.Read(0, 8)
				if !ok {
					failures.Add(1)
					return
				}
				first := vals[0]
				for _, v := range vals[1:] {
					if v != first {
						failures.Add(1)
						return
					}
				}
			}
		}()
	}
	wg.Wait()
	if n := failures.Load(); n > 0 {
		t.Fatalf("%d readers observed a torn/half batch", n)
	}
}
