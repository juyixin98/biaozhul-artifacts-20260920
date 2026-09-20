package service

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/jackc/pgx/v5"
	"signalboard/internal/db"
)

// SalesEventInput is a point-of-sale event. EventID is the caller-chosen
// idempotency key (unique per store); OccurredAt is when the sale actually
// happened and may be in the past (late events).
type SalesEventInput struct {
	EventID    string    `json:"event_id"`
	DishID     int64     `json:"dish_id"`
	Quantity   int32     `json:"quantity"`
	OccurredAt time.Time `json:"occurred_at"`
}

// SalesResult reports the state after applying the event.
type SalesResult struct {
	EventID   string    `json:"event_id"`
	DishID    int64     `json:"dish_id"`
	SalesDay  time.Time `json:"sales_day"` // store-local calendar date (00:00 UTC representation)
	DayTotal  int64     `json:"day_total"`
	SoldOut   bool      `json:"sold_out"`
	Threshold int64     `json:"threshold,omitempty"`
	Duplicate bool      `json:"duplicate,omitempty"`
}

// IngestSalesEvent applies one sales event.
//
// The whole operation is one transaction:
//   - the unique (store_id, event_id) index guarantees exactly-once counting
//     under concurrency (INSERT ... ON CONFLICT is not used, because a
//     duplicate id with a *different* payload must be reported as 409);
//   - the daily total upsert, threshold check and sellout flip happen on the
//     same snapshot so concurrent events can't be lost or double counted.
//
// Events are attributed to the store-local calendar date of occurred_at.
func (s *Service) IngestSalesEvent(ctx context.Context, storeID int64, in SalesEventInput) (SalesResult, error) {
	if in.EventID == "" {
		return SalesResult{}, fmt.Errorf("%w: event_id is required", ErrValidation)
	}
	if in.DishID <= 0 {
		return SalesResult{}, fmt.Errorf("%w: dish_id is required", ErrValidation)
	}
	if in.Quantity <= 0 {
		return SalesResult{}, fmt.Errorf("%w: quantity must be positive", ErrValidation)
	}
	if in.OccurredAt.IsZero() {
		return SalesResult{}, fmt.Errorf("%w: occurred_at is required", ErrValidation)
	}

	var res SalesResult
	err := s.tx(ctx, func(q *db.Queries) error {
		loc, err := s.storeLocation(ctx, q, storeID)
		if err != nil {
			return err
		}
		// Validate dish belongs to store (also defends FK on daily_sales).
		dish, err := q.GetDish(ctx, db.GetDishParams{ID: in.DishID, StoreID: storeID})
		if err != nil {
			if errors.Is(err, pgx.ErrNoRows) {
				return fmt.Errorf("%w: dish %d does not exist in store %d",
					ErrValidation, in.DishID, storeID)
			}
			return err
		}
		_ = dish

		// Idempotency: detect the existing event first. Unique index makes the
		// insert-vs-check race settle with 23505, which we turn into a payload
		// comparison below via a re-read.
		existing, err := q.GetSalesEvent(ctx, db.GetSalesEventParams{
			StoreID: storeID, EventID: in.EventID,
		})
		if err == nil {
			if existing.DishID != in.DishID ||
				existing.Quantity != in.Quantity ||
				!existing.OccurredAt.Time.UTC().Equal(in.OccurredAt.UTC()) {
				return fmt.Errorf("%w: event %s already recorded with different content",
					ErrConflict, in.EventID)
			}
			// Identical replay: no counting, return current state.
			day := storeLocalDay(in.OccurredAt, loc)
			res = SalesResult{
				EventID: in.EventID, DishID: in.DishID, SalesDay: day, Duplicate: true,
			}
			if ds, err := q.GetDailySales(ctx, db.GetDailySalesParams{
				StoreID: storeID, SalesDay: dateValue(day), DishID: in.DishID,
			}); err == nil {
				res.DayTotal = ds.Quantity
				res.SoldOut = ds.SoldOut
			}
			return nil
		} else if !errors.Is(err, pgx.ErrNoRows) {
			return err
		}

		if _, err := q.InsertSalesEvent(ctx, db.InsertSalesEventParams{
			StoreID: storeID, EventID: in.EventID, DishID: in.DishID,
			Quantity: in.Quantity, OccurredAt: timestamptz(in.OccurredAt),
		}); err != nil {
			if isPgCode(err, pgCodeUniqueViolation) {
				// Concurrent first-writer won; re-read to classify the duplicate.
				got, gErr := q.GetSalesEvent(ctx, db.GetSalesEventParams{
					StoreID: storeID, EventID: in.EventID,
				})
				if gErr != nil {
					return gErr
				}
				if got.DishID != in.DishID ||
					got.Quantity != in.Quantity ||
					!got.OccurredAt.Time.UTC().Equal(in.OccurredAt.UTC()) {
					return fmt.Errorf("%w: event %s already recorded with different content",
						ErrConflict, in.EventID)
				}
				return nil // identical concurrent replay; nothing more to do
			}
			return err
		}

		day := storeLocalDay(in.OccurredAt, loc)
		ds, err := q.UpsertDailySales(ctx, db.UpsertDailySalesParams{
			StoreID: storeID, SalesDay: dateValue(day),
			DishID: in.DishID, Quantity: int64(in.Quantity),
		})
		if err != nil {
			return err
		}

		res = SalesResult{
			EventID: in.EventID, DishID: in.DishID,
			SalesDay: day, DayTotal: ds.Quantity,
		}

		// Threshold lives on the immutable threshold table; its value at the
		// time of the sale decides sellout.
		if thr, err := q.GetThreshold(ctx, db.GetThresholdParams{
			StoreID: storeID, DishID: in.DishID,
		}); err == nil {
			res.Threshold = thr.Threshold
			if ds.Quantity >= thr.Threshold && !ds.SoldOut {
				if err := q.MarkSoldOut(ctx, db.MarkSoldOutParams{
					StoreID: storeID, SalesDay: dateValue(day), DishID: in.DishID,
				}); err != nil {
					return err
				}
				res.SoldOut = true
			} else {
				res.SoldOut = ds.SoldOut
			}
		} else if errors.Is(err, pgx.ErrNoRows) {
			res.SoldOut = false
		} else {
			return err
		}
		return nil
	})
	if err != nil {
		return SalesResult{}, err
	}
	return res, nil
}

// storeLocalDay truncates an instant to the calendar date in the store's zone.
// Returned time is midnight of that day; only the date portion is persisted.
func storeLocalDay(t time.Time, loc *time.Location) time.Time {
	y, m, d := t.In(loc).Date()
	return time.Date(y, m, d, 0, 0, 0, 0, time.UTC)
}

// IsSoldOut reports whether a dish is sold out for the store-local day of now.
// A new day naturally restores availability: rows are keyed by date.
func (s *Service) IsSoldOut(ctx context.Context, storeID, dishID int64, now time.Time) (bool, error) {
	loc, err := s.loadLocation(ctx, storeID)
	if err != nil {
		return false, err
	}
	day := storeLocalDay(now, loc)
	ds, err := s.q.GetDailySales(ctx, db.GetDailySalesParams{
		StoreID: storeID, SalesDay: dateValue(day), DishID: dishID,
	})
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return false, nil
		}
		return false, err
	}
	return ds.SoldOut, nil
}

func (s *Service) loadLocation(ctx context.Context, storeID int64) (*time.Location, error) {
	st, err := s.q.GetStore(ctx, storeID)
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return nil, ErrNotFound
		}
		return nil, err
	}
	return time.LoadLocation(st.Timezone)
}

// DishDayStat is one dish's totals for a day.
type DishDayStat struct {
	DishID   int64 `json:"dish_id"`
	Quantity int64 `json:"quantity"`
	SoldOut  bool  `json:"sold_out"`
}

// DaySales is the sales picture for one store-local date.
type DaySales struct {
	SalesDay time.Time     `json:"sales_day"`
	Stats    []DishDayStat `json:"stats"`
}

// SalesToday returns totals for the store-local day containing now.
func (s *Service) SalesToday(ctx context.Context, storeID int64, now time.Time) (DaySales, error) {
	loc, err := s.loadLocation(ctx, storeID)
	if err != nil {
		return DaySales{}, err
	}
	return s.salesForDay(ctx, storeID, storeLocalDay(now, loc))
}

// SalesTodayAt is SalesToday with an explicit clock (tests / backfills).
func (s *Service) SalesTodayAt(ctx context.Context, storeID int64, now time.Time) (DaySales, error) {
	return s.SalesToday(ctx, storeID, now)
}

func (s *Service) salesForDay(ctx context.Context, storeID int64, day time.Time) (DaySales, error) {
	rows, err := s.q.ListDailySalesForDay(ctx, db.ListDailySalesForDayParams{
		StoreID: storeID, SalesDay: dateValue(day),
	})
	if err != nil {
		return DaySales{}, err
	}
	out := DaySales{SalesDay: day, Stats: make([]DishDayStat, 0, len(rows))}
	for _, r := range rows {
		out.Stats = append(out.Stats, DishDayStat{
			DishID: r.DishID, Quantity: r.Quantity, SoldOut: r.SoldOut,
		})
	}
	return out, nil
}
