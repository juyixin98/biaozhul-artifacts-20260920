// Package api 提供 HTTP 摄入、修订、查询与管理接口。
package api

import (
	"encoding/json"
	"net/http"
	"strconv"
	"strings"

	"metricsink/internal/model"
	"metricsink/internal/store"
)

// Server 封装路由与存储句柄。
type Server struct {
	Store *store.Store
	Mux   *http.ServeMux
}

func NewServer(st *store.Store) *Server {
	s := &Server{Store: st, Mux: http.NewServeMux()}
	s.routes()
	return s
}

func (s *Server) routes() {
	s.Mux.HandleFunc("GET /healthz", s.health)
	s.Mux.HandleFunc("POST /v1/ingest", s.ingest)
	s.Mux.HandleFunc("POST /v1/delete", s.deleteSamples)
	s.Mux.HandleFunc("POST /v1/evict", s.evict)
	s.Mux.HandleFunc("GET /v1/series", s.listSeries)
	s.Mux.HandleFunc("GET /v1/query", s.query)
	s.Mux.HandleFunc("POST /v1/snapshot", s.snapshot)
}

func (s *Server) health(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

type ingestRequest struct {
	Samples []model.Sample `json:"samples"`
}

func (s *Server) ingest(w http.ResponseWriter, r *http.Request) {
	var req ingestRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "请求体不是合法 JSON: "+err.Error())
		return
	}
	rep, err := s.Store.Ingest(req.Samples)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	status := http.StatusOK
	if len(rep.Rejected) > 0 {
		status = http.StatusConflict
	}
	writeJSON(w, status, rep)
}

type deleteRequest struct {
	IDs []string `json:"ids"`
}

func (s *Server) deleteSamples(w http.ResponseWriter, r *http.Request) {
	var req deleteRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "请求体不是合法 JSON: "+err.Error())
		return
	}
	if len(req.IDs) == 0 {
		writeError(w, http.StatusBadRequest, "ids 不能为空")
		return
	}
	deleted, missing, err := s.Store.Delete(req.IDs)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	status := http.StatusOK
	if len(missing) > 0 {
		status = http.StatusNotFound
	}
	writeJSON(w, status, map[string]any{"deleted": deleted, "missing": missing})
}

type evictRequest struct {
	// Before 支持 Unix 秒或 RFC3339；早于该时刻的原始样本被驱逐。
	Before string `json:"before"`
}

func (s *Server) evict(w http.ResponseWriter, r *http.Request) {
	var req evictRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "请求体不是合法 JSON: "+err.Error())
		return
	}
	cutoff, err := model.ParseTs(req.Before)
	if err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	res, err := s.Store.Evict(cutoff)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, res)
}

func (s *Server) listSeries(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	sel := map[string]string{}
	for k, vs := range q {
		if strings.HasPrefix(k, "label.") && len(vs) > 0 {
			sel[strings.TrimPrefix(k, "label.")] = vs[0]
		}
	}
	views := s.Store.MatchSeries(q.Get("metric"), sel)
	writeJSON(w, http.StatusOK, map[string]any{"series": views})
}

// SeriesResult 是单条序列的查询结果。
type SeriesResult struct {
	Key    string            `json:"key"`
	Metric string            `json:"metric"`
	Labels map[string]string `json:"labels"`
	// Source 标识数据来源：raw（原始样本）、minutes、hours、
	// recompute-minutes/recompute-hours（对账用的原始重算）。
	Source string        `json:"source"`
	Points []model.Point `json:"points"`
}

func (s *Server) query(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	metric := q.Get("metric")
	if metric == "" {
		writeError(w, http.StatusBadRequest, "缺少 metric 参数")
		return
	}
	start, err := model.ParseTs(q.Get("start"))
	if err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	end, err := model.ParseTs(q.Get("end"))
	if err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	if end < start {
		writeError(w, http.StatusBadRequest, "end 早于 start")
		return
	}

	window, err := parseWindow(q.Get("step"), end-start)
	if err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	fill := q.Get("fill") == "zero"
	recompute := q.Get("source") == "recompute"

	sel := map[string]string{}
	for k, vs := range q {
		if strings.HasPrefix(k, "label.") && len(vs) > 0 {
			sel[strings.TrimPrefix(k, "label.")] = vs[0]
		}
	}

	views := s.Store.MatchSeries(metric, sel)
	results := make([]SeriesResult, 0, len(views))
	for _, v := range views {
		res := SeriesResult{Key: v.Key, Metric: v.Metric, Labels: v.Labels}
		switch {
		case window == 0:
			res.Source = "raw"
			raw := s.Store.RawPoints(v.Key, start, end)
			res.Points = rawToPoints(raw)
		case recompute && window == model.MinuteWindow:
			res.Source = "recompute-minutes"
			res.Points = s.Store.RecomputeFromRaw(v.Key, model.MinuteWindow, start, end, fill)
		case recompute && window == model.HourWindow:
			res.Source = "recompute-hours"
			res.Points = s.Store.RecomputeFromRaw(v.Key, model.HourWindow, start, end, fill)
		default:
			res.Source = sourceName(window)
			res.Points = s.Store.RollupPoints(v.Key, window, start, end, fill)
		}
		results = append(results, res)
	}
	writeJSON(w, http.StatusOK, map[string]any{"results": results})
}

func (s *Server) snapshot(w http.ResponseWriter, _ *http.Request) {
	if err := s.Store.SaveSnapshot(); err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"status": "snapshot saved"})
}

func sourceName(window int64) string {
	switch window {
	case model.MinuteWindow:
		return "minutes"
	case model.HourWindow:
		return "hours"
	default:
		return "custom"
	}
}

func rawToPoints(raw []model.Sample) []model.Point {
	out := make([]model.Point, 0, len(raw))
	for _, smp := range raw {
		v := smp.Value
		out = append(out, model.Point{
			Ts: smp.Ts, Count: 1, Sum: smp.Value, Min: smp.Value,
			Max: smp.Value, Avg: &v,
		})
	}
	return out
}

// parseWindow 解析 step 参数：
//
//	raw/0    -> 0（原始样本）
//	60/1m    -> 分钟层
//	3600/1h  -> 小时层
//	其他秒数 -> 自动选择容纳该步长的已存储层（演示查询路由）
func parseWindow(step string, span int64) (int64, error) {
	switch step {
	case "", "auto":
		switch {
		case span <= 2*model.HourWindow:
			return model.MinuteWindow, nil
		default:
			return model.HourWindow, nil
		}
	case "raw", "0":
		return 0, nil
	case "60", "1m":
		return model.MinuteWindow, nil
	case "3600", "1h":
		return model.HourWindow, nil
	}
	if n, err := strconv.ParseInt(step, 10, 64); err == nil {
		if n < 0 {
			return 0, &parseErr{"step 不能为负"}
		}
		switch {
		case n <= model.MinuteWindow:
			return model.MinuteWindow, nil
		default:
			return model.HourWindow, nil
		}
	}
	return 0, &parseErr{"无法解析 step（支持 raw/60/1m/3600/1h/秒数/auto）"}
}

type parseErr struct{ msg string }

func (e *parseErr) Error() string { return e.msg }

func writeError(w http.ResponseWriter, status int, msg string) {
	writeJSON(w, status, map[string]string{"error": msg})
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	enc := json.NewEncoder(w)
	enc.SetIndent("", "  ")
	_ = enc.Encode(v)
}
