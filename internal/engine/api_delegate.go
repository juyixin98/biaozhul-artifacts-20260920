package engine

import (
	"context"
	"fmt"
	"strconv"

	"resumable-bt/internal/store"
)

// GetVersion delegates to the store for the HTTP layer.
func (e *Engine) GetVersion(ctx context.Context, name string, versionStr string) (store.VersionRow, error) {
	version, err := parseVersion(versionStr)
	if err != nil {
		return store.VersionRow{}, err
	}
	return e.st.GetVersion(ctx, name, version)
}

// LatestVersion delegates to the store.
func (e *Engine) LatestVersion(ctx context.Context, name string) (int64, error) {
	return e.st.LatestVersion(ctx, name)
}

func parseVersion(s string) (int64, error) {
	v, err := strconv.ParseInt(s, 10, 64)
	if err != nil || v <= 0 {
		return 0, fmt.Errorf("version must be a positive integer")
	}
	return v, nil
}
