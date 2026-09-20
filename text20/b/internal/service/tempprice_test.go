package service_test

import (
	"context"
	"errors"
	"sync"
	"testing"
	"time"

	"signalboard/internal/db"
	"signalboard/internal/service"
	"signalboard/internal/testsupport"
)

func at(sec int64) time.Time { return time.Unix(sec, 0).UTC() }

// Half-open semantics: adjacent windows [0,100) and [100,200) do NOT overlap
// and both are accepted; crossing windows are rejected.
func TestTempPriceHalfOpenBoundaries(t *testing.T) {
	e := testsupport.New(t)
	ctx := context.Background()
	storeID, dishIDs := e.SeedStore(t, 1, "UTC")
	e.DraftAndPublish(t, storeID, dishIDs, 0)

	temps := []service.TempPriceInput{
		{DishID: dishIDs[0], Price: 100, StartsAt: at(0), EndsAt: at(100)},
		{DishID: dishIDs[0], Price: 200, StartsAt: at(100), EndsAt: at(200)},
	}
	if _, err := e.Svc.ScheduleTempPrices(ctx, storeID, temps); err != nil {
		t.Fatalf("adjacent half-open windows should be allowed: %v", err)
	}

	// Overlapping [0,100) vs [99,101).
	bad := []service.TempPriceInput{
		{DishID: dishIDs[0], Price: 100, StartsAt: at(0), EndsAt: at(100)},
		{DishID: dishIDs[0], Price: 200, StartsAt: at(99), EndsAt: at(101)},
	}
	if _, err := e.Svc.ScheduleTempPrices(ctx, storeID, bad); !errors.Is(err, service.ErrValidation) {
		t.Fatalf("overlapping windows: want ErrValidation, got %v", err)
	}

	// Empty/inverted interval rejected.
	inverted := []service.TempPriceInput{
		{DishID: dishIDs[0], Price: 100, StartsAt: at(100), EndsAt: at(100)},
	}
	if _, err := e.Svc.ScheduleTempPrices(ctx, storeID, inverted); !errors.Is(err, service.ErrValidation) {
		t.Fatalf("empty/equal interval: want ErrValidation, got %v", err)
	}
}

// Prices switch automatically at the boundaries with no republish.
func TestTempPriceAutoSwitchAtBoundaries(t *testing.T) {
	e := testsupport.New(t)
	ctx := context.Background()
	storeID, dishIDs := e.SeedStore(t, 1, "UTC")
	v := e.DraftAndPublish(t, storeID, dishIDs, 0)
	basePrice := v.Items[0].Price
	if basePrice != 100 {
		t.Fatalf("seed base price = %d, want 100", basePrice)
	}

	start := at(1000)
	end := at(2000)
	temps := []service.TempPriceInput{
		{DishID: dishIDs[0], Price: 55, StartsAt: start, EndsAt: end},
	}
	if _, err := e.Svc.ScheduleTempPrices(ctx, storeID, temps); err != nil {
		t.Fatal(err)
	}

	cases := []struct {
		name string
		now  time.Time
		want int64
	}{
		{"before window", start.Add(-time.Nanosecond), basePrice},
		{"at start (closed)", start, 55},
		{"inside", start.Add(time.Second), 55},
		{"at end (open)", end, basePrice},
		{"after end", end.Add(time.Nanosecond), basePrice},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			menu, err := e.Svc.GetScreenMenu(ctx, storeID, c.now)
			if err != nil {
				t.Fatal(err)
			}
			if got := menu.Lines[0].Price; got != c.want {
				t.Fatalf("price at %s = %d, want %d (no republish occurred)",
					c.now, got, c.want)
			}
		})
	}
}

// Concurrent schedules that overlap can't both win: the exclusion constraint
// makes one fail even though each passed app-level validation against the
// prior state.
func TestTempPriceConcurrentOverlapExcluded(t *testing.T) {
	e := testsupport.New(t)
	ctx := context.Background()
	storeID, dishIDs := e.SeedStore(t, 1, "UTC")
	v := e.DraftAndPublish(t, storeID, dishIDs, 0)

	run := func(price int64, startSec, finishSec int64) error {
		tx, err := e.Pool.Begin(ctx)
		if err != nil {
			return err
		}
		defer tx.Rollback(ctx)
		q := db.New(tx)
		// Deliberately do NOT take the service's store lock: this simulates a
		// writer that reached the table directly. Both transactions insert
		// overlapping windows and commit; the exclusion constraint must reject
		// the second committer regardless of application-level validation.
		if err := q.InsertTempPrice(ctx, db.InsertTempPriceParams{
			VersionID: v.ID, DishID: dishIDs[0], Price: price,
			StartsAt: dbTimestamptz(at(startSec)),
			EndsAt:   dbTimestamptz(at(finishSec)),
		}); err != nil {
			return err
		}
		// Hold the tx open briefly so both INSERTs are in flight before commit.
		time.Sleep(50 * time.Millisecond)
		return tx.Commit(ctx)
	}

	var wg sync.WaitGroup
	errs := make([]error, 2)
	begin := make(chan struct{})
	wg.Add(2)
	go func() { defer wg.Done(); <-begin; errs[0] = run(11, 100, 200) }()
	go func() { defer wg.Done(); <-begin; errs[1] = run(22, 150, 250) }()
	close(begin)
	wg.Wait()

	ok, failed := 0, 0
	for _, err := range errs {
		switch {
		case err == nil:
			ok++
		case isExclusion(err):
			failed++
		default:
			t.Fatalf("unexpected error: %v", err)
		}
	}
	if ok != 1 || failed != 1 {
		t.Fatalf("want exactly one overlap winner and one exclusion, got ok=%d failed=%d (errs=%v)",
			ok, failed, errs)
	}
}
