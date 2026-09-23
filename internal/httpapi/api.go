// Package httpapi 暴露基数预算治理服务的 HTTP 接口：
//
//	POST /ingest            摄入（支持单条与批量，Body 为 Sample 或 {"samples":[...]}）
//	GET  /metrics           列出指标名
//	GET  /metrics/{name}    查询单指标的组合、overflow 桶与预算占用
//	GET  /stats             全局计数与守恒统计
//	POST /admin/snapshot    立即落盘快照（需配置 snapshot 路径）
//	GET  /healthz           存活探针
package httpapi

import (
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"time"

	"cardinalgov/internal/governor"
)

// Server 持有 governor.Store 与可选的快照落盘函数。
type Server struct {
	Store        *governor.Store
	SaveSnapshot func() error // 由 main 注入；nil 时 /admin/snapshot 返回 503
}

// NewRouter 构造 http.Handler。
func (s *Server) NewRouter() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", s.handleHealth)
	mux.HandleFunc("POST /ingest", s.handleIngest)
	mux.HandleFunc("GET /metrics", s.handleMetrics)
	mux.HandleFunc("GET /metrics/{name}", s.handleQueryMetric)
	mux.HandleFunc("GET /stats", s.handleStats)
	mux.HandleFunc("POST /admin/snapshot", s.handleSnapshot)
	return mux
}

type ingestEnvelope struct {
	Samples []governor.Sample `json:"samples"`
}

type itemError struct {
	Index  int    `json:"index"`
	Reason string `json:"reason"`
}

type ingestResponse struct {
	Received   int               `json:"received"`
	Accepted   int               `json:"accepted"`
	Rejected   int               `json:"rejected"`
	Overflowed int               `json:"overflowed"`
	Results    []governor.Result `json:"results,omitempty"`
	Errors     []itemError       `json:"errors,omitempty"`
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	enc := json.NewEncoder(w)
	_ = enc.Encode(v)
}

func errorJSON(w http.ResponseWriter, status int, code, msg string) {
	writeJSON(w, status, map[string]string{"error": code, "message": msg})
}

func (s *Server) handleHealth(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

// parseSamples 同时接受单条对象和 {"samples":[...]} 批量对象。
func parseSamples(body []byte) ([]governor.Sample, error) {
	head := byte(0)
	for _, b := range body {
		switch b {
		case ' ', '\t', '\n', '\r':
			continue
		default:
			head = b
		}
		break
	}
	switch head {
	case '{':
		// 可能是单条 Sample，也可能是批量信封：先试信封。
		var env ingestEnvelope
		if err := json.Unmarshal(body, &env); err == nil && env.Samples != nil {
			return env.Samples, nil
		}
		var one governor.Sample
		if err := json.Unmarshal(body, &one); err != nil {
			return nil, err
		}
		return []governor.Sample{one}, nil
	case '[':
		var arr []governor.Sample
		if err := json.Unmarshal(body, &arr); err != nil {
			return nil, err
		}
		return arr, nil
	default:
		return nil, errors.New("body must be a JSON object or array")
	}
}

func (s *Server) handleIngest(w http.ResponseWriter, r *http.Request) {
	body, err := io.ReadAll(io.LimitReader(r.Body, 8<<20))
	if err != nil {
		errorJSON(w, http.StatusBadRequest, "read_failed", err.Error())
		return
	}
	samples, err := parseSamples(body)
	if err != nil {
		errorJSON(w, http.StatusBadRequest, "invalid_json", err.Error())
		return
	}
	if len(samples) == 0 {
		errorJSON(w, http.StatusBadRequest, "empty_batch", "no samples in request")
		return
	}

	now := time.Now().UnixMilli()
	resp := ingestResponse{Received: len(samples), Results: make([]governor.Result, 0, len(samples))}
	for i := range samples {
		if samples[i].Timestamp == 0 {
			samples[i].Timestamp = now
		}
		res := s.Store.Ingest(samples[i])
		if !res.Accepted {
			resp.Rejected++
			resp.Errors = append(resp.Errors, itemError{Index: i, Reason: res.Reason})
		} else {
			resp.Accepted++
			if res.Overflowed {
				resp.Overflowed++
			}
		}
		resp.Results = append(resp.Results, res)
	}
	// 存在拒绝项时返回 202（已处理但非全部摄入成功），全部成功为 200。
	status := http.StatusOK
	if resp.Rejected > 0 {
		status = http.StatusAccepted
	}
	writeJSON(w, status, resp)
}

func (s *Server) handleMetrics(w http.ResponseWriter, r *http.Request) {
	names := s.Store.MetricNames()
	writeJSON(w, http.StatusOK, map[string]any{"metrics": names, "count": len(names)})
}

func (s *Server) handleQueryMetric(w http.ResponseWriter, r *http.Request) {
	name := r.PathValue("name")
	view, ok := s.Store.QueryMetric(name)
	if !ok {
		errorJSON(w, http.StatusNotFound, "metric_not_found", "metric not found: "+name)
		return
	}
	writeJSON(w, http.StatusOK, view)
}

func (s *Server) handleStats(w http.ResponseWriter, r *http.Request) {
	stats := s.Store.Stats()
	resp := struct {
		governor.StatsView
		ConservationOK bool `json:"conservation_ok"`
	}{StatsView: stats, ConservationOK: stats.Counters.CheckConservation() == nil}
	writeJSON(w, http.StatusOK, resp)
}

func (s *Server) handleSnapshot(w http.ResponseWriter, r *http.Request) {
	if s.SaveSnapshot == nil {
		errorJSON(w, http.StatusServiceUnavailable, "snapshot_disabled", "snapshot path not configured")
		return
	}
	if err := s.SaveSnapshot(); err != nil {
		errorJSON(w, http.StatusInternalServerError, "snapshot_failed", err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"status": "saved"})
}
