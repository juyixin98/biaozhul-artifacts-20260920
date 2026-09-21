package httpapi_test

import (
	"net/http"

	"github.com/jackc/pgx/v5/pgxpool"

	httpapi "communityvault/internal/httpapi"
)

// newTestHandler wires the real router against the test pool with a short
// claim TTL so expiry scenarios run quickly.
func newTestHandler(pool *pgxpool.Pool) http.Handler {
	return httpapi.New(pool, 5 /* seconds */)
}
