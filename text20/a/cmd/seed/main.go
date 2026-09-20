// Command seed loads idempotent demo data: stores, dishes with limits, a
// published menu, temporary prices around "now", one provisioned screen per
// store, and a little sales history. It is safe to run repeatedly; on a clean
// database it creates the demo tenant, otherwise it leaves existing data.
package main

import (
	"context"
	"fmt"
	"log"
	"time"

	_ "time/tzdata"

	"github.com/jackc/pgx/v5"

	"signalboard/internal/config"
	"signalboard/internal/db"
	"signalboard/internal/pgdb"
)

func main() {
	cfg := config.Load()
	ctx := context.Background()

	pool, err := pgdb.Connect(ctx, cfg.DatabaseURL)
	if err != nil {
		log.Fatalf("connect: %v", err)
	}
	defer pool.Close()
	if _, err := pgdb.Migrate(ctx, pool); err != nil {
		log.Fatalf("migrate: %v", err)
	}

	q := db.New(pool)

	existing, err := q.ListStores(ctx)
	if err != nil {
		log.Fatalf("list stores: %v", err)
	}
	for _, st := range existing {
		if st.Name == "Demo Bistro" {
			log.Printf("demo data already present (store id=%d); nothing to do", st.ID)
			return
		}
	}

	store, err := q.CreateStore(ctx, db.CreateStoreParams{
		Name: "Demo Bistro", Timezone: "America/New_York",
	})
	if err != nil {
		log.Fatalf("create store: %v", err)
	}
	log.Printf("created store %d (%s)", store.ID, store.Timezone)

	type dish struct {
		sku, name string
		price     int64
		limit     *int64
		order     int32
	}
	intptr := func(v int64) *int64 { return &v }
	dishes := []dish{
		{"BK-001", "Classic Cheeseburger", 999, intptr(20), 1},
		{"BK-002", "Double Smash Burger", 1399, intptr(15), 2},
		{"SD-001", "Crispy Fries", 399, intptr(50), 3},
		{"DR-001", "Fresh Lemonade", 349, nil, 4},
		{"DS-001", "Brownie Sundae", 599, intptr(8), 5},
	}
	for _, d := range dishes {
		_, err := q.CreateDish(ctx, db.CreateDishParams{
			StoreID: store.ID, Sku: d.sku, Name: d.name, BasePrice: d.price,
			Active: true, DailyLimit: toInt8(d.limit), SortOrder: d.order,
		})
		if err != nil {
			log.Fatalf("create dish %s: %v", d.sku, err)
		}
	}
	log.Printf("created %d dishes", len(dishes))

	// Publish version 1 with temporary happy-hour prices bracketing now.
	tx, err := pool.Begin(ctx)
	if err != nil {
		log.Fatalf("begin: %v", err)
	}
	qtx := q.WithTx(tx)

	ver, err := qtx.CreateMenuVersion(ctx, db.CreateMenuVersionParams{
		StoreID: store.ID, Version: 1, Note: "initial demo menu",
	})
	if err != nil {
		log.Fatalf("version: %v", err)
	}
	active, err := qtx.ListActiveDishesForPublish(ctx, store.ID)
	if err != nil {
		log.Fatalf("list active: %v", err)
	}
	rows := make([]db.CopyVersionItemsParams, 0, len(active))
	for _, a := range active {
		rows = append(rows, db.CopyVersionItemsParams{
			VersionID: ver.ID, Sku: a.Sku, Name: a.Name,
			BasePrice: a.BasePrice, DisplayOrder: a.SortOrder,
		})
	}
	if _, err := qtx.CopyVersionItems(ctx, rows); err != nil {
		log.Fatalf("copy items: %v", err)
	}

	now := time.Now().UTC()
	happyHour := []struct {
		sku        string
		price      int64
		start, end time.Duration // relative to now
	}{
		{"DR-001", 249, -time.Hour, 2 * time.Hour},    // active now
		{"BK-001", 799, 3 * time.Hour, 6 * time.Hour}, // upcoming
		{"SD-001", 499, -3 * time.Hour, -time.Hour},   // just ended
	}
	for _, h := range happyHour {
		if err := qtx.InsertTemporaryPrice(ctx, db.InsertTemporaryPriceParams{
			StoreID: ver.StoreID, VersionID: ver.ID, Sku: h.sku, Price: h.price,
			StartAt: toTS(now.Add(h.start)), EndAt: toTS(now.Add(h.end)),
		}); err != nil {
			log.Fatalf("temp price %s: %v", h.sku, err)
		}
	}
	if err := qtx.BumpStoreVersion(ctx, db.BumpStoreVersionParams{
		ID: store.ID, MenuVersion: 1,
	}); err != nil {
		log.Fatalf("bump: %v", err)
	}
	if err := tx.Commit(ctx); err != nil {
		log.Fatalf("commit: %v", err)
	}
	log.Printf("published menu version 1 with %d items and %d temporary prices", len(rows), len(happyHour))

	// Provision one screen; print its token (stored only as a hash).
	token := "sbscr_demo_screen_token_change_me_0001"
	if _, err := q.CreateScreen(ctx, db.CreateScreenParams{
		StoreID: store.ID, Name: "Front Counter Display",
		TokenHash: sha256hex(token),
	}); err != nil {
		log.Fatalf("create screen: %v", err)
	}
	log.Printf("provisioned demo screen; token: %s", token)

	fmt.Println()
	fmt.Println("Demo ready.")
	fmt.Printf("  Admin base URL : http://localhost:8080/v1/admin/stores/%d\n", store.ID)
	fmt.Printf("  Admin token    : %s\n", cfg.AdminToken)
	fmt.Printf("  Screen token   : %s\n", token)
	fmt.Println("  Screen menu    : curl -H 'X-Screen-Token: " + token + "' http://localhost:8080/v1/screen/menu")

	_ = pgx.ErrNoRows
}
