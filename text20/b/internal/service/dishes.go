package service

import (
	"context"
	"errors"
	"time"

	"github.com/jackc/pgx/v5"
	"signalboard/internal/db"
)

type Dish struct {
	ID        int64     `json:"id"`
	StoreID   int64     `json:"store_id"`
	Name      string    `json:"name"`
	BasePrice int64     `json:"base_price"` // integer cents
	IsActive  bool      `json:"is_active"`
	CreatedAt time.Time `json:"created_at"`
	UpdatedAt time.Time `json:"updated_at"`
}

func toDish(r db.Dish) Dish {
	return Dish{
		ID: r.ID, StoreID: r.StoreID, Name: r.Name,
		BasePrice: r.BasePrice, IsActive: r.IsActive,
		CreatedAt: r.CreatedAt.Time, UpdatedAt: r.UpdatedAt.Time,
	}
}

func validatePrice(cents int64) error {
	if cents < 0 {
		return errors.New("price must be non-negative integer cents")
	}
	return nil
}

func (s *Service) CreateDish(ctx context.Context, storeID int64, name string, basePrice int64) (Dish, error) {
	if name == "" {
		return Dish{}, errors.New("name is required")
	}
	if err := validatePrice(basePrice); err != nil {
		return Dish{}, err
	}
	if _, err := s.GetStore(ctx, storeID); err != nil {
		return Dish{}, err
	}
	row, err := s.q.CreateDish(ctx, db.CreateDishParams{
		StoreID: storeID, Name: name, BasePrice: basePrice,
	})
	if err != nil {
		if isPgCode(err, pgCodeUniqueViolation) {
			return Dish{}, ErrConflict
		}
		return Dish{}, err
	}
	return toDish(row), nil
}

func (s *Service) ListDishes(ctx context.Context, storeID int64) ([]Dish, error) {
	if _, err := s.GetStore(ctx, storeID); err != nil {
		return nil, err
	}
	rows, err := s.q.ListDishes(ctx, storeID)
	if err != nil {
		return nil, err
	}
	out := make([]Dish, 0, len(rows))
	for _, r := range rows {
		out = append(out, toDish(r))
	}
	return out, nil
}

func (s *Service) SetDishPrice(ctx context.Context, storeID, dishID int64, price int64) error {
	if err := validatePrice(price); err != nil {
		return err
	}
	rows, err := s.q.SetDishPrice(ctx, db.SetDishPriceParams{
		ID: dishID, StoreID: storeID, BasePrice: price,
	})
	if err != nil {
		return err
	}
	if rows == 0 {
		return ErrNotFound
	}
	return nil
}

func (s *Service) SetDishActive(ctx context.Context, storeID, dishID int64, active bool) error {
	rows, err := s.q.SetDishActive(ctx, db.SetDishActiveParams{
		ID: dishID, StoreID: storeID, IsActive: active,
	})
	if err != nil {
		return err
	}
	if rows == 0 {
		return ErrNotFound
	}
	return nil
}

// SetThreshold configures the daily sellout quantity for a dish.
func (s *Service) SetThreshold(ctx context.Context, storeID, dishID, threshold int64) error {
	if threshold <= 0 {
		return errors.New("threshold must be positive")
	}
	if _, err := s.GetStore(ctx, storeID); err != nil {
		return err
	}
	if _, err := s.q.GetDish(ctx, db.GetDishParams{ID: dishID, StoreID: storeID}); err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return ErrNotFound
		}
		return err
	}
	_, err := s.q.UpsertThreshold(ctx, db.UpsertThresholdParams{
		StoreID: storeID, DishID: dishID, Threshold: threshold,
	})
	return err
}
