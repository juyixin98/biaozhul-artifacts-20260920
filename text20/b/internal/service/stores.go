package service

import (
	"context"
	"errors"
	"time"

	"github.com/jackc/pgx/v5/pgtype"

	"github.com/jackc/pgx/v5"
	"signalboard/internal/db"
)

type Store struct {
	ID        int64     `json:"id"`
	Name      string    `json:"name"`
	Timezone  string    `json:"timezone"`
	CreatedAt time.Time `json:"created_at"`
}

func toStore(r db.Store) Store {
	return Store{ID: r.ID, Name: r.Name, Timezone: r.Timezone, CreatedAt: r.CreatedAt.Time}
}

func (s *Service) CreateStore(ctx context.Context, name, timezone string) (Store, error) {
	if name == "" {
		return Store{}, errors.New("name is required")
	}
	if _, err := time.LoadLocation(timezone); err != nil {
		return Store{}, errors.New("invalid IANA timezone: " + timezone)
	}
	row, err := s.q.CreateStore(ctx, db.CreateStoreParams{Name: name, Timezone: timezone})
	if err != nil {
		return Store{}, err
	}
	return toStore(row), nil
}

func (s *Service) GetStore(ctx context.Context, id int64) (Store, error) {
	row, err := s.q.GetStore(ctx, id)
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return Store{}, ErrNotFound
		}
		return Store{}, err
	}
	return toStore(row), nil
}

func (s *Service) ListStores(ctx context.Context) ([]Store, error) {
	rows, err := s.q.ListStores(ctx)
	if err != nil {
		return nil, err
	}
	out := make([]Store, 0, len(rows))
	for _, r := range rows {
		out = append(out, toStore(r))
	}
	return out, nil
}

// storeLocation loads the store and returns its time zone; missing store -> 404.
func (s *Service) storeLocation(ctx context.Context, q *db.Queries, storeID int64) (*time.Location, error) {
	r, err := q.GetStore(ctx, storeID)
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return nil, ErrNotFound
		}
		return nil, err
	}
	loc, err := time.LoadLocation(r.Timezone)
	if err != nil {
		return nil, err
	}
	return loc, nil
}

func timestamptz(t time.Time) pgtype.Timestamptz {
	return pgtype.Timestamptz{Time: t.UTC(), Valid: true}
}

func dateValue(t time.Time) pgtype.Date {
	return pgtype.Date{Time: t, Valid: true}
}
