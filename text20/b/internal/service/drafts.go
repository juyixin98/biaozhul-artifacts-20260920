package service

import (
	"context"
	"errors"
	"fmt"

	"github.com/jackc/pgx/v5"
	"signalboard/internal/db"
)

const MaxBatchItems = 500

// DraftItemInput is one line of a draft put or batch import.
// Price nil means "follow catalog base price".
type DraftItemInput struct {
	DishID int64  `json:"dish_id"`
	Price  *int64 `json:"price,omitempty"`
}

// DraftItem is a joined draft line for API responses.
type DraftItem struct {
	DishID    int64  `json:"dish_id"`
	DishName  string `json:"dish_name"`
	Position  int32  `json:"position"`
	Price     *int64 `json:"price,omitempty"`
	BasePrice int64  `json:"base_price"`
}

type Draft struct {
	Version int64       `json:"version"` // draft edit counter
	Items   []DraftItem `json:"items"`
}

// GetDraft returns the store's draft, creating an empty one on first access.
func (s *Service) GetDraft(ctx context.Context, storeID int64) (Draft, error) {
	if _, err := s.GetStore(ctx, storeID); err != nil {
		return Draft{}, err
	}
	d, err := s.q.GetOrCreateDraft(ctx, storeID)
	if err != nil {
		return Draft{}, err
	}
	rows, err := s.q.ListDraftItems(ctx, d.ID)
	if err != nil {
		return Draft{}, err
	}
	return Draft{Version: d.Version, Items: toDraftItems(rows)}, nil
}

func toDraftItems(rows []db.ListDraftItemsRow) []DraftItem {
	out := make([]DraftItem, 0, len(rows))
	for _, r := range rows {
		out = append(out, DraftItem{
			DishID: r.DishID, DishName: r.DishName, Position: r.Position,
			Price: r.Price, BasePrice: r.BasePrice,
		})
	}
	return out
}

// ReplaceDraft rewrites the whole draft atomically: either every item is
// accepted or none are. It is also the implementation of batch import.
func (s *Service) ReplaceDraft(ctx context.Context, storeID int64, items []DraftItemInput) (Draft, error) {
	if len(items) > MaxBatchItems {
		return Draft{}, fmt.Errorf("%w: batch size %d exceeds maximum %d",
			ErrValidation, len(items), MaxBatchItems)
	}
	err := s.tx(ctx, func(q *db.Queries) error {
		if _, err := s.storeLocation(ctx, q, storeID); err != nil {
			return err
		}
		draft, err := q.GetOrCreateDraft(ctx, storeID)
		if err != nil {
			return err
		}
		if err := q.ClearDraftItems(ctx, draft.ID); err != nil {
			return err
		}
		seen := make(map[int64]bool, len(items))
		for i, it := range items {
			if it.DishID <= 0 {
				return fmt.Errorf("%w: item %d: dish_id is required", ErrValidation, i)
			}
			if seen[it.DishID] {
				return fmt.Errorf("%w: item %d: duplicate dish_id %d",
					ErrValidation, i, it.DishID)
			}
			seen[it.DishID] = true
			if it.Price != nil {
				if err := validatePrice(*it.Price); err != nil {
					return fmt.Errorf("%w: item %d (dish %d): %v",
						ErrValidation, i, it.DishID, err)
				}
			}
			dish, err := q.GetDish(ctx, db.GetDishParams{ID: it.DishID, StoreID: storeID})
			if err != nil {
				if errors.Is(err, pgx.ErrNoRows) {
					return fmt.Errorf("%w: item %d: dish %d does not exist in store %d",
						ErrValidation, i, it.DishID, storeID)
				}
				return err
			}
			if !dish.IsActive {
				return fmt.Errorf("%w: item %d: dish %d is inactive",
					ErrValidation, i, it.DishID)
			}
			if _, err := q.InsertDraftItem(ctx, db.InsertDraftItemParams{
				DraftID: draft.ID, DishID: it.DishID,
				Position: int32(i), Price: it.Price,
			}); err != nil {
				return fmt.Errorf("%w: item %d: %v", ErrValidation, i, err)
			}
		}
		return q.BumpDraftVersion(ctx, storeID)
	})
	if err != nil {
		return Draft{}, err
	}
	return s.GetDraft(ctx, storeID)
}
