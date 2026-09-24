package server

import (
	"log"
	"net/http"
	"os"
)

// httpLog is the access logger used by WithLogging.
var httpLog = log.New(os.Stdout, "hlc-http ", log.LstdFlags)

// WithLogging emits one minimal access-log line per request.
func WithLogging(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		rec := &statusRecorder{ResponseWriter: w, status: http.StatusOK}
		next.ServeHTTP(rec, r)
		httpLog.Printf("%s %s %d", r.Method, r.URL.RequestURI(), rec.status)
	})
}
