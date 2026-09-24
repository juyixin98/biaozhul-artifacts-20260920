package httpapi

import (
	"log/slog"
	"net/http"
	"os"
	"time"
)

func getenvDefault(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}

// crashProcess 模拟“指针提交前进程崩溃”：立即以非零码退出。
// 已复制校验的副本保留在磁盘、in_progress 尝试保留在库中，由下次启动恢复。
func crashProcess() {
	slog.Error("FAULT INJECTION: simulated crash before pointer commit; exiting 99",
		"pid", os.Getpid())
	os.Exit(99)
}

type statusWriter struct {
	http.ResponseWriter
	code int
}

func (w *statusWriter) WriteHeader(code int) {
	w.code = code
	w.ResponseWriter.WriteHeader(code)
}

func requestLogger(log *slog.Logger) func(http.Handler) http.Handler {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			start := time.Now()
			sw := &statusWriter{ResponseWriter: w, code: http.StatusOK}
			next.ServeHTTP(sw, r)
			log.Info("http", "method", r.Method, "path", r.URL.Path,
				"status", sw.code, "dur", time.Since(start).String())
		})
	}
}
