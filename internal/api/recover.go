package api

import (
	"net/http"

	"github.com/go-chi/chi/v5/middleware"
)

// recoverer converts panics into 500s and never leaks internals.
func recoverer(next http.Handler) http.Handler {
	return middleware.Recoverer(next)
}
