package api

import (
	"errors"
	"io"
	"net/http"

	"github.com/clearsettle/clearsettle/internal/service"
)

// idemFromRequest reads the Idempotency-Key header and the raw body, hashing
// the body canonically. The body bytes are returned so handlers can decode
// them afterwards.
func idemFromRequest(r *http.Request) (service.Idem, []byte, error) {
	key := r.Header.Get("Idempotency-Key")
	if key == "" {
		return service.Idem{}, nil, errors.New("Idempotency-Key header required")
	}
	body, err := io.ReadAll(r.Body)
	if err != nil {
		return service.Idem{}, nil, err
	}
	hash, err := service.CanonicalRequestHash(body)
	if err != nil {
		return service.Idem{}, nil, err
	}
	return service.Idem{Key: key, RequestHash: hash}, body, nil
}
