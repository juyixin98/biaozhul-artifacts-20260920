package service

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/jackc/pgx/v5"
	"signalboard/internal/db"
)

// TempPriceInput carries a scheduled price inside a publish or schedule call.
// StartsAt/EndsAt are RFC3339 timestamps with offset; intervals are
// half-open [start, end) in UTC internally.
type TempPriceInput struct {
	DishID   int64     `json:"dish_id"`
	Price    int64     `json:"price"` // cents
	StartsAt time.Time `json:"starts_at"`
	EndsAt   time.Time `json:"ends_at"`
}

type TempPrice struct {
	ID       int64     `json:"id"`
	DishID   int64     `json:"dish_id"`
	Price    int64     `json:"price"`
	StartsAt time.Time `json:"starts_at"`
	EndsAt   time.Time `json:"ends_at"`
}

type MenuVersion struct {
	ID          int64           `json:"id"`
	StoreID     int64           `json:"store_id"`
	Version     int64           `json:"version"`
	PublishedAt time.Time       `json:"published_at"`
	Items       []PublishedItem `json:"items"`
	TempPrices  []TempPrice     `json:"temp_prices,omitempty"`
}

type PublishedItem struct {
	DishID   int64  `json:"dish_id"`
	DishName string `json:"dish_name"`
	Position int32  `json:"position"`
	Price    int64  `json:"price"` // snapshot price at publish, cents
}

// lockStore takes a row lock on the store to serialize publishes and
// temp-price schedules for that store.
func lockStore(ctx context.Context, q *db.Queries, storeID int64) error {
	if err := q.LockStore(ctx, storeID); err != nil {
		return fmt.Errorf("locking store %d: %w", storeID, err)
	}
	return nil
}

// validateTempWindows checks half-open, non-overlapping windows per dish.
// Pure application-level validation for friendly errors; the database carries
// an exclusion constraint as the concurrency-proof backstop.
func validateTempWindows(in []TempPriceInput) error {
	type win struct {
		dish       int64
		start, end time.Time
	}
	wins := make([]win, 0, len(in))
	for i, t := range in {
		if t.Price < 0 {
			return fmt.Errorf("%w: temp price %d: negative price", ErrValidation, i)
		}
		st := t.StartsAt.UTC()
		en := t.EndsAt.UTC()
		if !st.Before(en) {
			return fmt.Errorf("%w: temp price %d: starts_at must be before ends_at (half-open)",
				ErrValidation, i)
		}
		wins = append(wins, win{dish: t.DishID, start: st, end: en})
	}
	for i := 0; i < len(wins); i++ {
		for j := i + 1; j < len(wins); j++ {
			if wins[i].dish != wins[j].dish {
				continue
			}
			// overlap test for half-open intervals: start < other.end && other.start < end
			if wins[i].start.Before(wins[j].end) && wins[j].start.Before(wins[i].end) {
				return fmt.Errorf("%w: dish %d has overlapping temp-price windows [%s,%s) vs [%s,%s)",
					ErrValidation, wins[i].dish,
					wins[i].start.Format(time.RFC3339), wins[i].end.Format(time.RFC3339),
					wins[j].start.Format(time.RFC3339), wins[j].end.Format(time.RFC3339))
			}
		}
	}
	return nil
}

// Publish turns the current draft into an immutable snapshot.
//
// expectedVersion is the caller's view of the current published version
// (0 if the store has never published). The store row is locked for the
// duration so that two concurrent publishes carrying the same expected
// version serialize: the first commits version+1, and the second observes
// the new current version and fails with ErrPrecondition.
func (s *Service) Publish(ctx context.Context, storeID, expectedVersion int64, publishedBy string, temps []TempPriceInput) (MenuVersion, error) {
	if err := validateTempWindows(temps); err != nil {
		return MenuVersion{}, err
	}
	var result MenuVersion
	err := s.tx(ctx, func(q *db.Queries) error {
		if _, err := s.storeLocation(ctx, q, storeID); err != nil {
			return err
		}
		// Serialize concurrent publishers per store.
		if err := lockStore(ctx, q, storeID); err != nil {
			return err
		}

		current, err := q.GetLatestPublishedVersion(ctx, storeID)
		currentVersion := int64(0)
		if err == nil {
			currentVersion = current.Version
		} else if !errors.Is(err, pgx.ErrNoRows) {
			return err
		}
		if currentVersion != expectedVersion {
			return fmt.Errorf("%w: expected published version %d, current is %d",
				ErrPrecondition, expectedVersion, currentVersion)
		}

		draft, err := q.GetOrCreateDraft(ctx, storeID)
		if err != nil {
			return err
		}
		draftRows, err := q.ListDraftItems(ctx, draft.ID)
		if err != nil {
			return err
		}
		if len(draftRows) == 0 {
			return fmt.Errorf("%w: refusing to publish an empty menu", ErrValidation)
		}
		// Dish IDs referenced by temp prices must be in the published set.
		inMenu := make(map[int64]bool, len(draftRows))
		for _, r := range draftRows {
			inMenu[r.DishID] = true
		}
		for _, t := range temps {
			if !inMenu[t.DishID] {
				return fmt.Errorf("%w: temp price dish %d is not in the draft menu",
					ErrValidation, t.DishID)
			}
		}

		v, err := q.InsertMenuVersion(ctx, db.InsertMenuVersionParams{
			StoreID: storeID, Version: currentVersion + 1, PublishedBy: publishedBy,
		})
		if err != nil {
			return err
		}
		for _, r := range draftRows {
			price := r.BasePrice
			if r.Price != nil {
				price = *r.Price
			}
			if err := q.InsertMenuVersionItem(ctx, db.InsertMenuVersionItemParams{
				VersionID: v.ID, DishID: r.DishID, DishName: r.DishName,
				Position: r.Position, Price: price,
			}); err != nil {
				return err
			}
		}
		for _, t := range temps {
			if err := q.InsertTempPrice(ctx, db.InsertTempPriceParams{
				VersionID: v.ID, DishID: t.DishID, Price: t.Price,
				StartsAt: timestamptz(t.StartsAt), EndsAt: timestamptz(t.EndsAt),
			}); err != nil {
				if isPgCode(err, pgCodeExclusionViolation) {
					return fmt.Errorf("%w: concurrent temp price overlaps an existing window",
						ErrConflict)
				}
				return err
			}
		}

		loaded, err := loadVersion(ctx, q, v.ID)
		if err != nil {
			return err
		}
		result = loaded
		return nil
	})
	if err != nil {
		return MenuVersion{}, err
	}
	return result, nil
}

func loadVersion(ctx context.Context, q *db.Queries, versionID int64) (MenuVersion, error) {
	// Items/temp rows are loaded via version id; header fetch goes through a
	// direct lookup using the per-store version table.
	return loadVersionScoped(ctx, q, versionID)
}

func loadVersionScoped(ctx context.Context, q *db.Queries, versionID int64) (MenuVersion, error) {
	items, err := q.ListVersionItems(ctx, versionID)
	if err != nil {
		return MenuVersion{}, err
	}
	temps, err := q.ListTempPricesForVersion(ctx, versionID)
	if err != nil {
		return MenuVersion{}, err
	}
	// Header: get via list? We fetch by joining store through a dedicated query.
	header, err := q.GetVersionByID(ctx, versionID)
	if err != nil {
		return MenuVersion{}, err
	}
	out := MenuVersion{
		ID: header.ID, StoreID: header.StoreID, Version: header.Version,
		PublishedAt: header.PublishedAt.Time,
		Items:       make([]PublishedItem, 0, len(items)),
		TempPrices:  make([]TempPrice, 0, len(temps)),
	}
	for _, r := range items {
		out.Items = append(out.Items, PublishedItem{
			DishID: r.DishID, DishName: r.DishName, Position: r.Position, Price: r.Price,
		})
	}
	for _, r := range temps {
		out.TempPrices = append(out.TempPrices, TempPrice{
			ID: r.ID, DishID: r.DishID, Price: r.Price,
			StartsAt: r.StartsAt.Time.UTC(), EndsAt: r.EndsAt.Time.UTC(),
		})
	}
	return out, nil
}

// GetPublishedMenu loads a specific published version, or the latest when
// version <= 0.
func (s *Service) GetPublishedMenu(ctx context.Context, storeID, version int64) (MenuVersion, error) {
	if _, err := s.GetStore(ctx, storeID); err != nil {
		return MenuVersion{}, err
	}
	var versionID int64
	if version <= 0 {
		v, err := s.q.GetLatestPublishedVersion(ctx, storeID)
		if err != nil {
			if errors.Is(err, pgx.ErrNoRows) {
				return MenuVersion{}, ErrNotFound
			}
			return MenuVersion{}, err
		}
		versionID = v.ID
	} else {
		v, err := s.q.GetPublishedVersionByNumber(ctx,
			db.GetPublishedVersionByNumberParams{StoreID: storeID, Version: version})
		if err != nil {
			if errors.Is(err, pgx.ErrNoRows) {
				return MenuVersion{}, ErrNotFound
			}
			return MenuVersion{}, err
		}
		versionID = v.ID
	}
	return loadVersionScoped(ctx, s.q, versionID)
}

// ScheduleTempPrices replaces the temp-price schedule of the *latest*
// published version. Older immutable versions are never touched. The
// exclusion constraint makes overlapping concurrent schedules fail even if
// both passed application validation.
func (s *Service) ScheduleTempPrices(ctx context.Context, storeID int64, temps []TempPriceInput) (MenuVersion, error) {
	if err := validateTempWindows(temps); err != nil {
		return MenuVersion{}, err
	}
	var result MenuVersion
	err := s.tx(ctx, func(q *db.Queries) error {
		if _, err := s.storeLocation(ctx, q, storeID); err != nil {
			return err
		}
		if err := lockStore(ctx, q, storeID); err != nil {
			return err
		}
		latest, err := q.GetLatestPublishedVersion(ctx, storeID)
		if err != nil {
			if errors.Is(err, pgx.ErrNoRows) {
				return ErrNotFound
			}
			return err
		}
		items, err := q.ListVersionItems(ctx, latest.ID)
		if err != nil {
			return err
		}
		inMenu := map[int64]bool{}
		for _, it := range items {
			inMenu[it.DishID] = true
		}
		for _, t := range temps {
			if !inMenu[t.DishID] {
				return fmt.Errorf("%w: temp price dish %d is not in the current published menu",
					ErrValidation, t.DishID)
			}
		}
		if err := q.DeleteTempPricesForVersion(ctx, latest.ID); err != nil {
			return err
		}
		for _, t := range temps {
			if err := q.InsertTempPrice(ctx, db.InsertTempPriceParams{
				VersionID: latest.ID, DishID: t.DishID, Price: t.Price,
				StartsAt: timestamptz(t.StartsAt), EndsAt: timestamptz(t.EndsAt),
			}); err != nil {
				if isPgCode(err, pgCodeExclusionViolation) {
					return fmt.Errorf("%w: concurrent temp price overlaps an existing window",
						ErrConflict)
				}
				return err
			}
		}
		loaded, err := loadVersionScoped(ctx, q, latest.ID)
		if err != nil {
			return err
		}
		result = loaded
		return nil
	})
	if err != nil {
		return MenuVersion{}, err
	}
	return result, nil
}
