package attest

import (
	"encoding/json"
	"io"
	"log"
	"net/http"
	"time"
)

const maxBodyBytes = 1 << 20 // 1 MiB

// NewHandler 返回验签服务的 HTTP 路由。
func NewHandler(verifier *Verifier) http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("POST /v1/verify", func(w http.ResponseWriter, r *http.Request) {
		body, err := io.ReadAll(http.MaxBytesReader(w, r.Body, maxBodyBytes))
		if err != nil {
			writeJSON(w, http.StatusRequestEntityTooLarge, Result{Accepted: false, Reason: "请求体过大或读取失败"})
			return
		}
		res := verifier.Verify(body)
		status := http.StatusOK
		if !res.Accepted {
			status = http.StatusUnprocessableEntity
		}
		writeJSON(w, status, res)
	})
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
	})
	return mux
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	if err := json.NewEncoder(w).Encode(v); err != nil {
		log.Printf("写响应失败: %v", err)
	}
}

// Server 是带超时的 http.Server 便捷封装。
func Server(addr string, verifier *Verifier) *http.Server {
	return &http.Server{
		Addr:              addr,
		Handler:           NewHandler(verifier),
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       10 * time.Second,
		WriteTimeout:      10 * time.Second,
		IdleTimeout:       60 * time.Second,
	}
}
