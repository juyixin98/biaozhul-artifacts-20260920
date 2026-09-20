package httpapi

import (
	"log"
	"net/http"
	"runtime/debug"

	"sircc/internal/httpx"
)

func requestLogger(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		ww := &statusWriter{ResponseWriter: w, status: http.StatusOK}
		next.ServeHTTP(ww, r)
		log.Printf("%s %s -> %d (request_id=%q user=%q)",
			r.Method, r.URL.Path, ww.status, r.Header.Get("X-Request-Id"), r.Header.Get("X-User-Id"))
	})
}

func recoverer(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		defer func() {
			if rec := recover(); rec != nil {
				log.Printf("panic: %v\n%s", rec, debug.Stack())
				httpx.ErrorJSON(w, http.StatusInternalServerError, httpx.CodeInternal, "internal error")
			}
		}()
		next.ServeHTTP(w, r)
	})
}

type statusWriter struct {
	http.ResponseWriter
	status int
}

func (s *statusWriter) WriteHeader(code int) {
	s.status = code
	s.ResponseWriter.WriteHeader(code)
}
