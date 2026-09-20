// Package seed loads a small, self-consistent demo data set: one store in
// New York (with a UTC offset boundary case), a handful of dishes, a draft,
// a published menu with happy-hour temp prices, sellout thresholds, and one
// registered screen. It is safe to run on an empty database only.
package seed

import (
	"context"
	"fmt"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"

	"signalboard/internal/service"
)

type Result struct {
	StoreID          int64
	DishIDs          []int64
	PublishedVersion int64
	ScreenID         int64
	ScreenToken      string
}

func Run(ctx context.Context, pool *pgxpool.Pool) (Result, error) {
	svc := service.New(pool)
	r := Result{}

	st, err := svc.CreateStore(ctx, "Signal Burger NYC", "America/New_York")
	if err != nil {
		return r, fmt.Errorf("create store: %w", err)
	}
	r.StoreID = st.ID

	dishSpecs := []struct {
		name  string
		price int64
	}{
		{"Classic Burger", 999},
		{"Cheeseburger", 1099},
		{"Fries", 399},
		{"Soda", 249},
	}
	for _, d := range dishSpecs {
		dish, err := svc.CreateDish(ctx, st.ID, d.name, d.price)
		if err != nil {
			return r, fmt.Errorf("create dish: %w", err)
		}
		r.DishIDs = append(r.DishIDs, dish.ID)
	}

	// Sellout threshold: cheeseburgers sell out after 50/day.
	if err := svc.SetThreshold(ctx, st.ID, r.DishIDs[1], 50); err != nil {
		return r, fmt.Errorf("threshold: %w", err)
	}

	items := make([]service.DraftItemInput, len(r.DishIDs))
	for i, id := range r.DishIDs {
		items[i] = service.DraftItemInput{DishID: id}
	}
	if _, err := svc.ReplaceDraft(ctx, st.ID, items); err != nil {
		return r, fmt.Errorf("draft: %w", err)
	}

	// Happy-hour windows relative to now so the demo shows a live temp price:
	// the first window brackets the current time; the second is later today.
	now := time.Now().UTC()
	hour := time.Hour
	temps := []service.TempPriceInput{
		{
			DishID: r.DishIDs[2], Price: 299, // fries on sale now
			StartsAt: now.Add(-time.Hour), EndsAt: now.Add(time.Hour),
		},
		{
			DishID: r.DishIDs[2], Price: 199, // later window, adjacent (not overlap)
			StartsAt: now.Add(time.Hour), EndsAt: now.Add(3 * hour),
		},
	}
	v, err := svc.Publish(ctx, st.ID, 0, "seed", temps)
	if err != nil {
		return r, fmt.Errorf("publish: %w", err)
	}
	r.PublishedVersion = v.Version

	screenID, token, err := svc.RegisterScreen(ctx, st.ID, "front-counter-1")
	if err != nil {
		return r, fmt.Errorf("screen: %w", err)
	}
	r.ScreenID = screenID
	r.ScreenToken = token

	return r, nil
}
