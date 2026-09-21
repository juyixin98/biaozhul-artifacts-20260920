package server

import (
	"context"
	"fmt"
	"io"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"

	"signalboard/internal/db"
)

// SeedDemo loads a small demo dataset (one store, four items, one lunch
// temp-price band, published as version 1, one screen) and prints the
// screen token. It is a no-op when any store already exists.
func SeedDemo(ctx context.Context, pool *pgxpool.Pool, q *db.Queries, out io.Writer) error {
	var n int
	if err := pool.QueryRow(ctx, `SELECT count(*) FROM stores`).Scan(&n); err != nil {
		return err
	}
	if n > 0 {
		fmt.Fprintln(out, "seed: stores already exist, skipping demo data")
		return nil
	}

	store, err := q.CreateStore(ctx, db.CreateStoreParams{Name: "Demo Downtown", Timezone: "Asia/Shanghai"})
	if err != nil {
		return err
	}
	if err := q.EnsureDraft(ctx, store.ID); err != nil {
		return err
	}
	items := []db.UpsertDraftItemParams{
		{StoreID: store.ID, ItemKey: "classic-burger", Name: "Classic Burger", PriceCents: 1299, SoldOutThreshold: 50, Position: 1},
		{StoreID: store.ID, ItemKey: "fries", Name: "Fries", PriceCents: 499, SoldOutThreshold: 100, Position: 2},
		{StoreID: store.ID, ItemKey: "cola", Name: "Cola", PriceCents: 299, SoldOutThreshold: 0, Position: 3},
		{StoreID: store.ID, ItemKey: "latte", Name: "Latte", PriceCents: 1599, SoldOutThreshold: 30, Position: 4},
	}
	for _, it := range items {
		if _, err := q.UpsertDraftItem(ctx, it); err != nil {
			return err
		}
	}
	now := time.Now().UTC()
	if _, err := q.CreateDraftTempPrice(ctx, db.CreateDraftTempPriceParams{
		StoreID:    store.ID,
		ItemKey:    "classic-burger",
		PriceCents: 999,
		StartsAt:   now.Add(-time.Hour),
		EndsAt:     now.Add(23 * time.Hour),
	}); err != nil {
		return err
	}
	mv, err := (&Server{q: q, pool: pool}).publishSnapshot(ctx, store.ID, 0)
	if err != nil {
		return err
	}
	token, err := newScreenToken()
	if err != nil {
		return err
	}
	screen, err := q.CreateScreen(ctx, db.CreateScreenParams{
		StoreID:   store.ID,
		Name:      "front-counter-1",
		TokenHash: hashToken(token),
	})
	if err != nil {
		return err
	}

	fmt.Fprintf(out, `seed: demo data loaded
  store:   %s (%s, timezone %s)
  version: %d published
  screen:  %s (%s)
  token:   %s

Try:
  curl -H "Authorization: Bearer %s" http://localhost:8080/screen/menu
`, uuidString(store.ID), store.Name, store.Timezone, mv.Version,
		uuidString(screen.ID), screen.Name, token, token)
	return nil
}
