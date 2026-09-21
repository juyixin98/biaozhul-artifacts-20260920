package server

import (
	"context"
	"errors"
	"net/http"

	"github.com/jackc/pgx/v5/pgtype"

	"signalboard/internal/db"
)

// ErrVersionConflict is returned when expected_version does not match the
// store's current version (another publisher won the race).
var ErrVersionConflict = errors.New("version conflict")

// publishSnapshot atomically claims the next version number and copies the
// current draft (items + temp prices) into a new immutable menu version.
// The draft itself is left untouched.
func (s *Server) publishSnapshot(ctx context.Context, storeID pgtype.UUID, expectedVersion int32) (db.MenuVersion, error) {
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return db.MenuVersion{}, err
	}
	defer func() { _ = tx.Rollback(ctx) }()
	q := s.q.WithTx(tx)

	// UPDATE ... WHERE current_version = expected takes a row lock; of two
	// concurrent publishers with the same expectation exactly one updates a
	// row, the other gets ErrNoRows.
	newVersion, err := q.BumpStoreVersion(ctx, db.BumpStoreVersionParams{
		ID:             storeID,
		CurrentVersion: expectedVersion,
	})
	if isNoRows(err) {
		return db.MenuVersion{}, ErrVersionConflict
	}
	if err != nil {
		return db.MenuVersion{}, err
	}
	mv, err := q.CreateMenuVersion(ctx, db.CreateMenuVersionParams{StoreID: storeID, Version: newVersion})
	if err != nil {
		return db.MenuVersion{}, err
	}
	if err := q.CopyDraftItemsToVersion(ctx, db.CopyDraftItemsToVersionParams{VersionID: mv.ID, StoreID: storeID}); err != nil {
		return db.MenuVersion{}, err
	}
	if err := q.CopyDraftTempPricesToVersion(ctx, db.CopyDraftTempPricesToVersionParams{VersionID: mv.ID, StoreID: storeID}); err != nil {
		return db.MenuVersion{}, err
	}
	if err := tx.Commit(ctx); err != nil {
		return db.MenuVersion{}, err
	}
	return mv, nil
}

type publishRequest struct {
	ExpectedVersion int32 `json:"expected_version"`
}

func (s *Server) publish(w http.ResponseWriter, r *http.Request) {
	storeID, ok := uuidParam(w, r, "storeID")
	if !ok {
		return
	}
	var req publishRequest
	if !decodeJSON(w, r, &req) {
		return
	}
	if _, err := s.q.GetStore(r.Context(), storeID); isNoRows(err) {
		writeError(w, http.StatusNotFound, "store not found")
		return
	}
	mv, err := s.publishSnapshot(r.Context(), storeID, req.ExpectedVersion)
	if errors.Is(err, ErrVersionConflict) {
		writeError(w, http.StatusConflict, "expected_version does not match the store's current version")
		return
	}
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusCreated, map[string]any{
		"id":           uuidString(mv.ID),
		"store_id":     uuidString(mv.StoreID),
		"version":      mv.Version,
		"published_at": mv.PublishedAt,
	})
}
