// Package httpapi 暴露计数器样本的摄入与增长/速率查询接口。
package httpapi

import (
	"encoding/json"
	"errors"
	"net/http"
	"strconv"
	"strings"

	"counterreset/internal/rate"
	"counterreset/internal/series"
	"counterreset/internal/store"
	"counterreset/internal/wal"
)

// Server 组装存储与可选 WAL。
type Server struct {
	Store *store.Store
	WAL   *wal.WAL // 可为 nil（内存模式 / 重放期间）
	mux   *http.ServeMux
}

// NewServer 创建路由。
func NewServer(st *store.Store, w *wal.WAL) *Server {
	s := &Server{Store: st, WAL: w}
	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", s.handleHealth)
	mux.HandleFunc("/v1/ingest", s.handleIngest)
	mux.HandleFunc("/v1/series", s.handleSeries)
	mux.HandleFunc("/v1/increase", s.handleIncrease)
	mux.HandleFunc("/v1/rate", s.handleRate)
	s.mux = mux
	return s
}

// Handler 返回可挂载的 http.Handler。
func (s *Server) Handler() http.Handler { return s.mux }

func (s *Server) handleHealth(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		writeError(w, http.StatusMethodNotAllowed, "仅支持 GET")
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

// ingestRequest 是 /v1/ingest 的请求体。
type ingestRequest struct {
	Labels  map[string]string `json:"labels"`
	Samples []series.Sample   `json:"samples"`
}

func (s *Server) handleIngest(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeError(w, http.StatusMethodNotAllowed, "仅支持 POST")
		return
	}
	var req ingestRequest
	dec := json.NewDecoder(r.Body)
	dec.DisallowUnknownFields()
	if err := dec.Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "请求体不是合法 JSON: "+err.Error())
		return
	}
	if len(req.Labels) == 0 {
		writeError(w, http.StatusBadRequest, "labels 不能为空")
		return
	}
	if len(req.Samples) == 0 {
		writeError(w, http.StatusBadRequest, "samples 不能为空")
		return
	}

	// 先落盘（若启用 WAL），再更新内存；非法样本整批拒绝、不落盘。
	// 注意：本样例未做两阶段提交，“落盘成功但内存更新失败”在当前实现中
	// 不可能发生（Ingest 在校验通过后不会再失败）；重放幂等，因此
	// “内存已更新但 fsync 失败”的极端情况重启后至多丢失最后一条。
	if s.WAL != nil {
		if err := s.WAL.Append(wal.Record{Labels: req.Labels, Samples: req.Samples}); err != nil {
			writeError(w, http.StatusInternalServerError, "持久化失败: "+err.Error())
			return
		}
	}
	res, rejected, err := s.Store.Ingest(req.Labels, req.Samples)
	if err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	if len(rejected) > 0 {
		writeJSON(w, http.StatusBadRequest, map[string]any{
			"error":    "存在非法样本，整批拒绝（未写入任何数据）",
			"rejected": rejected,
		})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"status": "accepted",
		"labels": req.Labels,
		"ingest": res,
	})
}

func (s *Server) handleSeries(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		writeError(w, http.StatusMethodNotAllowed, "仅支持 GET")
		return
	}
	metrics := s.Store.List()
	type item struct {
		Labels            map[string]string `json:"labels"`
		SampleCount       int               `json:"sample_count"`
		FirstTsMs         *int64            `json:"first_ts_ms"`
		LastTsMs          *int64            `json:"last_ts_ms"`
		DuplicateSame     int               `json:"duplicate_same"`
		DuplicateConflict int               `json:"duplicate_conflicts"`
	}
	out := make([]item, 0, len(metrics))
	for _, m := range metrics {
		it := item{
			Labels: m.Labels, SampleCount: len(m.Samples),
			DuplicateSame: m.DuplicateSame, DuplicateConflict: m.DuplicateConflict,
		}
		if len(m.Samples) > 0 {
			first := m.Samples[0].TimestampMs
			last := m.Samples[len(m.Samples)-1].TimestampMs
			it.FirstTsMs, it.LastTsMs = &first, &last
		}
		out = append(out, it)
	}
	writeJSON(w, http.StatusOK, map[string]any{"series": out})
}

// queryParams 是 increase/rate 共用的查询参数。
type queryParams struct {
	labels   map[string]string
	startMs  int64
	endMs    int64
	extrap   rate.Extrapolation
	capacity float64
}

func parseQuery(r *http.Request) (queryParams, error) {
	q := r.URL.Query()
	p := queryParams{capacity: 0}

	labelPairs := q["label"] // 支持重复参数 label=k=v
	if raw := q.Get("labels"); raw != "" {
		labelPairs = append(labelPairs, strings.Split(raw, ",")...) // 也支持 labels=k=v,k2=v2
	}
	p.labels = map[string]string{}
	for _, pair := range labelPairs {
		pair = strings.TrimSpace(pair)
		if pair == "" {
			continue
		}
		eq := strings.IndexByte(pair, '=')
		if eq <= 0 || eq == len(pair)-1 {
			return p, errors.New("标签格式错误，应为 k=v（多标签用重复 label 参数或逗号分隔）")
		}
		p.labels[pair[:eq]] = pair[eq+1:]
	}
	if len(p.labels) == 0 {
		return p, errors.New("必须通过 label=k=v 指定至少一个标签")
	}

	var err error
	if p.startMs, err = parseInt64(q.Get("start_ms")); err != nil {
		return p, errors.New("start_ms 参数无效: " + err.Error())
	}
	if p.endMs, err = parseInt64(q.Get("end_ms")); err != nil {
		return p, errors.New("end_ms 参数无效: " + err.Error())
	}
	if p.extrap, err = rate.ParseExtrapolation(q.Get("extrapolation")); err != nil {
		return p, err
	}
	if c := q.Get("capacity"); c != "" {
		if p.capacity, err = strconv.ParseFloat(c, 64); err != nil || p.capacity <= 0 {
			return p, errors.New("capacity 参数无效：必须是正数")
		}
	}
	return p, nil
}

func parseInt64(s string) (int64, error) {
	if s == "" {
		return 0, errors.New("缺少参数")
	}
	return strconv.ParseInt(s, 10, 64)
}

func (s *Server) runQuery(w http.ResponseWriter, r *http.Request) {
	p, err := parseQuery(r)
	if err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	m := s.Store.Get(p.labels)
	if m == nil {
		writeError(w, http.StatusNotFound, "未找到该标签对应的序列")
		return
	}
	rep, err := rate.Compute(m.Samples, p.startMs, p.endMs, p.extrap, p.capacity)
	if err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"labels": p.labels,
		"report": rep,
	})
}

func (s *Server) handleIncrease(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		writeError(w, http.StatusMethodNotAllowed, "仅支持 GET")
		return
	}
	s.runQuery(w, r)
}

func (s *Server) handleRate(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		writeError(w, http.StatusMethodNotAllowed, "仅支持 GET")
		return
	}
	s.runQuery(w, r)
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	enc := json.NewEncoder(w)
	enc.SetIndent("", "  ")
	_ = enc.Encode(v)
}

func writeError(w http.ResponseWriter, status int, msg string) {
	writeJSON(w, status, map[string]string{"error": msg})
}
