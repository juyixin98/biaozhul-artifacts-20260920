package server

import (
	"errors"
	"io"
	"net/http"
)

// decodeLimit reads at most maxBodyBytes from the request body. A body that is
// too large yields an error rather than being silently truncated.
func decodeLimit(w http.ResponseWriter, r *http.Request) ([]byte, error) {
	r.Body = http.MaxBytesReader(w, r.Body, maxBodyBytes)
	body, err := io.ReadAll(r.Body)
	var maxErr *http.MaxBytesError
	if errors.As(err, &maxErr) {
		return nil, errors.New("request body too large (max 1 MiB)")
	}
	if err != nil {
		return nil, errors.New("failed to read request body")
	}
	return body, nil
}
