// Package api 提供离线扩缩容控制器的 HTTP 接口（仅标准库 net/http）。
package api

import (
	"encoding/json"
	"log"
	"net/http"

	"offline-scaler/internal/scaler"
)

// maxBodyBytes 限制单次回放请求体大小，防止异常输入耗尽内存。
const maxBodyBytes = 10 << 20 // 10 MiB

// Handler 组装全部路由。
func Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			writeError(w, http.StatusMethodNotAllowed, "仅支持 GET")
			return
		}
		writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
	})
	mux.HandleFunc("/api/v1/replay", replayHandler)
	mux.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/" {
			writeError(w, http.StatusNotFound, "未找到该路径；可用接口: POST /api/v1/replay, GET /healthz")
			return
		}
		writeJSON(w, http.StatusOK, map[string]any{
			"service": "offline-scaler",
			"endpoints": map[string]string{
				"POST /api/v1/replay": "离线回放：给定 config/samples/decisionTimes，返回每步决策与依据",
				"GET /healthz":        "健康检查",
			},
		})
	})
	return loggingMiddleware(mux)
}

func replayHandler(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		w.Header().Set("Allow", http.MethodPost)
		writeError(w, http.StatusMethodNotAllowed, "仅支持 POST")
		return
	}
	r.Body = http.MaxBytesReader(w, r.Body, maxBodyBytes)

	dec := json.NewDecoder(r.Body)
	dec.DisallowUnknownFields()
	var req scaler.ReplayRequest
	if err := dec.Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "请求 JSON 解析失败: "+err.Error())
		return
	}

	result, err := scaler.Replay(req)
	if err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"result": result})
}

func loggingMiddleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		log.Printf("%s %s", r.Method, r.URL.Path)
		next.ServeHTTP(w, r)
	})
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	enc := json.NewEncoder(w)
	enc.SetIndent("", "  ")
	if err := enc.Encode(v); err != nil {
		log.Printf("写响应失败: %v", err)
	}
}

func writeError(w http.ResponseWriter, status int, msg string) {
	writeJSON(w, status, map[string]string{"error": msg})
}
