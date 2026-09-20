package service

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"time"

	"github.com/jackc/pgx/v5"
	"signalboard/internal/db"
)

// OfflineThreshold is how long without a heartbeat before a screen is offline.
const OfflineThreshold = 90 * time.Second

// ScreenMenuLine is one rendered line on a display.
type ScreenMenuLine struct {
	DishID  int64  `json:"dish_id"`
	Name    string `json:"name"`
	Price   int64  `json:"price"` // currently effective price, cents
	SoldOut bool   `json:"sold_out"`
}

// ScreenMenu is the complete state a display needs at one instant. It is
// assembled inside a single transaction/serializable snapshot so that a
// response never mixes two states.
type ScreenMenu struct {
	StoreID     int64            `json:"store_id"`
	Version     int64            `json:"version"` // published menu version
	PublishedAt time.Time        `json:"published_at"`
	GeneratedAt time.Time        `json:"generated_at"`
	Lines       []ScreenMenuLine `json:"lines"`
}

func hashToken(token string) string {
	sum := sha256.Sum256([]byte(token))
	return hex.EncodeToString(sum[:])
}

// GenerateScreenToken returns a fresh opaque token shown to the operator once.
func GenerateScreenToken() string {
	b := make([]byte, 32)
	if _, err := rand.Read(b); err != nil {
		panic(err)
	}
	return hex.EncodeToString(b)
}

// RegisterScreen provisions a display bound to one store.
func (s *Service) RegisterScreen(ctx context.Context, storeID int64, name string) (screenID int64, token string, err error) {
	if _, err := s.GetStore(ctx, storeID); err != nil {
		return 0, "", err
	}
	if name == "" {
		name = "screen"
	}
	token = GenerateScreenToken()
	row, err := s.q.CreateScreen(ctx, db.CreateScreenParams{
		StoreID: storeID, Name: name, TokenHash: hashToken(token),
	})
	if err != nil {
		return 0, "", err
	}
	return row.ID, token, nil
}

// AuthenticateScreen resolves a bearer token to a screen row.
func (s *Service) AuthenticateScreen(ctx context.Context, token string) (db.Screen, error) {
	row, err := s.q.GetScreenByTokenHash(ctx, hashToken(token))
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return db.Screen{}, ErrUnauthorized
		}
		return db.Screen{}, err
	}
	return row, nil
}

// Heartbeat marks a screen alive; its store ownership is checked by the caller.
func (s *Service) Heartbeat(ctx context.Context, screenID int64) (online bool, err error) {
	if err := s.q.TouchScreen(ctx, screenID); err != nil {
		return false, err
	}
	return true, nil
}

// ScreenStatus describes a provisioned display for management APIs.
type ScreenStatus struct {
	ID         int64     `json:"id"`
	StoreID    int64     `json:"store_id"`
	Name       string    `json:"name"`
	LastSeenAt time.Time `json:"last_seen_at"`
	Online     bool      `json:"online"`
}

func (s *Service) ListScreens(ctx context.Context, storeID int64, now time.Time) ([]ScreenStatus, error) {
	if _, err := s.GetStore(ctx, storeID); err != nil {
		return nil, err
	}
	rows, err := s.q.ListScreensByStore(ctx, storeID)
	if err != nil {
		return nil, err
	}
	out := make([]ScreenStatus, 0, len(rows))
	for _, r := range rows {
		out = append(out, ScreenStatus{
			ID: r.ID, StoreID: r.StoreID, Name: r.Name,
			LastSeenAt: r.LastSeenAt.Time,
			Online:     now.Sub(r.LastSeenAt.Time) <= OfflineThreshold,
		})
	}
	return out, nil
}

// GetScreenMenu assembles the effective menu for one store at time now.
//
// Serialization is performed in a REPEATABLE READ transaction: the version
// header, line items, active temporary prices and today's sellout flags are
// all read from the same snapshot, so the response can never interleave two
// published/sales states.
func (s *Service) GetScreenMenu(ctx context.Context, storeID int64, now time.Time) (ScreenMenu, error) {
	var menu ScreenMenu
	err := pgxRepeatableRead(ctx, s.pool, func(q *db.Queries) error {
		v, err := q.GetLatestPublishedVersion(ctx, storeID)
		if err != nil {
			if errors.Is(err, pgx.ErrNoRows) {
				return ErrNotFound
			}
			return err
		}
		items, err := q.ListVersionItems(ctx, v.ID)
		if err != nil {
			return err
		}
		actives, err := q.ActiveTempPricesAt(ctx, db.ActiveTempPricesAtParams{
			VersionID: v.ID, StartsAt: timestamptz(now),
		})
		if err != nil {
			return err
		}

		loc, err := s.storeLocation(ctx, q, storeID)
		if err != nil {
			return err
		}
		day := storeLocalDay(now, loc)

		menu = ScreenMenu{
			StoreID: storeID, Version: v.Version,
			PublishedAt: v.PublishedAt.Time, GeneratedAt: now.UTC(),
			Lines: make([]ScreenMenuLine, 0, len(items)),
		}
		for _, it := range items {
			price := it.Price
			soldOut := false
			ds, err := q.GetDailySales(ctx, db.GetDailySalesParams{
				StoreID: storeID, SalesDay: dateValue(day), DishID: it.DishID,
			})
			if err != nil && !errors.Is(err, pgx.ErrNoRows) {
				return err
			}
			if err == nil {
				soldOut = ds.SoldOut
			}
			menu.Lines = append(menu.Lines, ScreenMenuLine{
				DishID: it.DishID, Name: it.DishName,
				Price: price, SoldOut: soldOut,
			})
		}
		// Apply active temporary prices (at most one per dish due to the
		// exclusion constraint + half-open windows).
		priceByDish := map[int64]int64{}
		for _, a := range actives {
			priceByDish[a.DishID] = a.Price
		}
		for i := range menu.Lines {
			if p, ok := priceByDish[menu.Lines[i].DishID]; ok {
				menu.Lines[i].Price = p
			}
		}
		return nil
	})
	if err != nil {
		return ScreenMenu{}, err
	}
	return menu, nil
}
