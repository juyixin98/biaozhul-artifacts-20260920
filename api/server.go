// Package api 把 quorum.Cluster 暴露为纯后端 HTTP/JSON 接口。
//
// 所有接口均返回 JSON。写/读在法定人数未达成时返回 504（但响应体
// 仍包含真实的部分结果）；读发现兄弟版本（冲突）时返回 409；
// key 在法定人数内不存在时返回 404。
package api

import (
	"encoding/json"
	"fmt"
	"net/http"
	"strings"
	"time"

	"quorum-demo/quorum"
)

// Server 持有一个被模拟的集群。
type Server struct {
	cluster *quorum.Cluster
	mux     *http.ServeMux
}

// NewServer 构造 HTTP 服务。
func NewServer(c *quorum.Cluster) *Server {
	s := &Server{cluster: c, mux: http.NewServeMux()}
	s.routes()
	return s
}

// Handler 返回可挂载的 http.Handler。
func (s *Server) Handler() http.Handler { return s.mux }

func (s *Server) routes() {
	s.mux.HandleFunc("GET /health", s.handleHealth)
	s.mux.HandleFunc("GET /config", s.handleGetConfig)
	s.mux.HandleFunc("POST /config", s.handleSetConfig)
	s.mux.HandleFunc("POST /replicas/down", s.handleDown)
	s.mux.HandleFunc("POST /replicas/up", s.handleUp)
	s.mux.HandleFunc("POST /write", s.handleWrite)
	s.mux.HandleFunc("POST /read", s.handleRead)
	s.mux.HandleFunc("POST /resolve", s.handleResolve)
	s.mux.HandleFunc("POST /repair", s.handleRepair)
	s.mux.HandleFunc("GET /state", s.handleState)
	s.mux.HandleFunc("POST /reset", s.handleReset)
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	enc := json.NewEncoder(w)
	enc.SetIndent("", "  ")
	_ = enc.Encode(v)
}

func writeError(w http.ResponseWriter, status int, format string, args ...any) {
	writeJSON(w, status, map[string]string{"error": fmt.Sprintf(format, args...)})
}

func decode(w http.ResponseWriter, r *http.Request, dst any) bool {
	dec := json.NewDecoder(r.Body)
	if err := dec.Decode(dst); err != nil {
		writeError(w, http.StatusBadRequest, "请求体不是合法 JSON: %v", err)
		return false
	}
	return true
}

// clockInput 是向量钟在请求 JSON 中的形态，例如 {"n1":1,"n2":3}。
type clockInput = map[string]int

// parentsPayload 用于读入携带父钟的请求。
type writePayload struct {
	Key         string       `json:"key"`
	Value       string       `json:"value"`
	Coordinator string       `json:"coordinator"`
	Parents     []clockInput `json:"parents"`
	Nodes       []string     `json:"nodes"`
	DeadlineMS  int          `json:"deadline_ms"`
}

type readPayload struct {
	Key        string   `json:"key"`
	Nodes      []string `json:"nodes"`
	NoRepair   bool     `json:"no_repair"`
	DeadlineMS int      `json:"deadline_ms"`
}

type resolvePayload struct {
	Key         string       `json:"key"`
	Value       string       `json:"value"`
	Coordinator string       `json:"coordinator"`
	Siblings    []clockInput `json:"siblings"` // 需要消解的兄弟版本钟；等价于写的 parents
	Nodes       []string     `json:"nodes"`
	DeadlineMS  int          `json:"deadline_ms"`
}

type repairPayload struct {
	Key   string   `json:"key"`
	Nodes []string `json:"nodes"`
}

type nodePayload struct {
	Node string `json:"node"`
}

type configPayload struct {
	W int `json:"w"`
	R int `json:"r"`
}

func toClocks(in []clockInput) []quorum.Clock {
	out := make([]quorum.Clock, 0, len(in))
	for _, m := range in {
		c := quorum.Clock{}
		for k, v := range m {
			c[k] = v
		}
		out = append(out, c)
	}
	return out
}

func (s *Server) handleHealth(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{
		"status": "ok",
		"time":   time.Now().UTC().Format(time.RFC3339Nano),
	})
}

func (s *Server) handleGetConfig(w http.ResponseWriter, r *http.Request) {
	st := s.cluster.State("")
	writeJSON(w, http.StatusOK, map[string]any{
		"config": st.Config,
		"nodes":  s.cluster.Nodes(),
		"down":   st.Down,
	})
}

func (s *Server) handleSetConfig(w http.ResponseWriter, r *http.Request) {
	var p configPayload
	if !decode(w, r, &p) {
		return
	}
	if err := s.cluster.Reconfigure(p.W, p.R); err != nil {
		writeError(w, http.StatusBadRequest, "%v", err)
		return
	}
	st := s.cluster.State("")
	writeJSON(w, http.StatusOK, map[string]any{
		"config": st.Config,
		"note":   quorumNote(st.Config),
	})
}

func quorumNote(c quorum.Config) string {
	if c.W+c.R > c.N {
		return fmt.Sprintf("W+R=%d > N=%d：任意读写法定人数有交集，读集合中至少有一个副本参与过最近一次已完成的写。但这不消解并发写冲突，也不自动产生线性一致历史（见 README）。", c.W+c.R, c.N)
	}
	return fmt.Sprintf("W+R=%d <= N=%d：读写法定人数可能不相交，即使没有并发写也可能读到旧值。", c.W+c.R, c.N)
}

func (s *Server) handleDown(w http.ResponseWriter, r *http.Request) {
	var p nodePayload
	if !decode(w, r, &p) {
		return
	}
	if err := s.cluster.SetDown(strings.TrimSpace(p.Node)); err != nil {
		writeError(w, http.StatusBadRequest, "%v", err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"down": s.cluster.DownSet()})
}

func (s *Server) handleUp(w http.ResponseWriter, r *http.Request) {
	var p nodePayload
	if !decode(w, r, &p) {
		return
	}
	if err := s.cluster.SetUp(strings.TrimSpace(p.Node)); err != nil {
		writeError(w, http.StatusBadRequest, "%v", err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"down": s.cluster.DownSet(),
		"note": "副本已恢复在线：它带着停机前的旧数据回到集群，后续读修复或 /repair 会将其追平。",
	})
}

func (s *Server) handleWrite(w http.ResponseWriter, r *http.Request) {
	var p writePayload
	if !decode(w, r, &p) {
		return
	}
	resp, err := s.cluster.Write(quorum.WriteRequest{
		Key:         p.Key,
		Value:       p.Value,
		Coordinator: p.Coordinator,
		Parents:     toClocks(p.Parents),
		Nodes:       p.Nodes,
		DeadlineMS:  p.DeadlineMS,
	})
	if err != nil {
		writeError(w, http.StatusBadRequest, "%v", err)
		return
	}
	status := http.StatusOK
	if !resp.QuorumMet {
		status = http.StatusGatewayTimeout // 504：部分写已发生
	}
	writeJSON(w, status, resp)
}

func (s *Server) handleRead(w http.ResponseWriter, r *http.Request) {
	var p readPayload
	if !decode(w, r, &p) {
		return
	}
	resp, err := s.cluster.Read(quorum.ReadRequest{
		Key:        p.Key,
		Nodes:      p.Nodes,
		NoRepair:   p.NoRepair,
		DeadlineMS: p.DeadlineMS,
	})
	if err != nil {
		writeError(w, http.StatusBadRequest, "%v", err)
		return
	}
	switch {
	case !resp.QuorumMet:
		writeJSON(w, http.StatusGatewayTimeout, resp)
	case resp.NotFound:
		writeJSON(w, http.StatusNotFound, resp)
	case resp.Conflict:
		writeJSON(w, http.StatusConflict, resp)
	default:
		writeJSON(w, http.StatusOK, resp)
	}
}

func (s *Server) handleResolve(w http.ResponseWriter, r *http.Request) {
	var p resolvePayload
	if !decode(w, r, &p) {
		return
	}
	if len(p.Siblings) == 0 {
		writeError(w, http.StatusBadRequest, "resolve 需要在 siblings 中给出至少一个要消解的兄弟版本向量钟")
		return
	}
	resp, err := s.cluster.Resolve(quorum.ResolveRequest{
		Key:         p.Key,
		Value:       p.Value,
		Coordinator: p.Coordinator,
		Clocks:      toClocks(p.Siblings),
		Nodes:       p.Nodes,
		DeadlineMS:  p.DeadlineMS,
	})
	if err != nil {
		writeError(w, http.StatusBadRequest, "%v", err)
		return
	}
	status := http.StatusOK
	if !resp.QuorumMet {
		status = http.StatusGatewayTimeout
	}
	writeJSON(w, status, resp)
}

func (s *Server) handleRepair(w http.ResponseWriter, r *http.Request) {
	var p repairPayload
	if !decode(w, r, &p) {
		return
	}
	resp, err := s.cluster.AntiEntropy(quorum.RepairRequest{Key: p.Key, Nodes: p.Nodes})
	if err != nil {
		writeError(w, http.StatusBadRequest, "%v", err)
		return
	}
	writeJSON(w, http.StatusOK, resp)
}

func (s *Server) handleState(w http.ResponseWriter, r *http.Request) {
	key := r.URL.Query().Get("key")
	writeJSON(w, http.StatusOK, s.cluster.State(key))
}

func (s *Server) handleReset(w http.ResponseWriter, r *http.Request) {
	s.cluster.Reset()
	writeJSON(w, http.StatusOK, map[string]string{"status": "reset"})
}
