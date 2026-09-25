package server

import (
	"log"
	"net/http"
	"strconv"
	"time"
)

// timeconv parses a base-10, 64-bit integer (used for limit and
// after_seq query parameters).
func timeconv(s string) (int, error) {
	return strconv.Atoi(s)
}

type statusRecorder struct {
	http.ResponseWriter
	status int
}

func (r *statusRecorder) WriteHeader(code int) {
	r.status = code
	r.ResponseWriter.WriteHeader(code)
}

// logFiles is a minimal structured request logger writing one line per
// request to the standard logger.
func logRequests(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		rec := &statusRecorder{ResponseWriter: w, status: http.StatusOK}
		next.ServeHTTP(rec, r)
		log.Printf("http method=%s path=%s status=%d dur=%s remote=%s",
			r.Method, r.URL.Path, rec.status, time.Since(start), r.RemoteAddr)
	})
}
