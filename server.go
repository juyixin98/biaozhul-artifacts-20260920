// server.go — net/http 接口层。仅用标准库，把 Cluster 模拟能力暴露成 HTTP API。
package main

import (
	"encoding/json"
	"errors"
	"net/http"
	"strconv"
	"strings"
	"time"
)

// Server 包装 Cluster，提供 HTTP 处理函数。
type Server struct {
	cluster *Cluster
}

func NewServer(c *Cluster) *Server { return &Server{cluster: c} }

// Mux 注册全部路由（Go 1.22+ 的方法路由）。
func (s *Server) Mux() *http.ServeMux {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /config", s.handleConfig)
	mux.HandleFunc("PUT /config", s.handleConfig)
	mux.HandleFunc("POST /write", s.handleWrite)
	mux.HandleFunc("GET /read", s.handleRead)
	mux.HandleFunc("POST /resolve", s.handleResolve)
	mux.HandleFunc("POST /fault", s.handleFault)
	mux.HandleFunc("POST /recover", s.handleRecover)
	mux.HandleFunc("GET /state", s.handleState)
	mux.HandleFunc("POST /reset", s.handleReset)
	return mux
}

// writeRequest 是 POST /write 的请求体。
type writeRequest struct {
	Key         string `json:"key"`
	Value       string `json:"value"`
	Context     Clock  `json:"context"` // 因果向量时钟；空/省略表示新分支
	Coordinator int    `json:"coordinator"`
	W           int    `json:"w"`
	Targets     []int  `json:"targets"` // 只向指定副本发意向（故障演练用）
	TimeoutMS   int    `json:"timeout_ms"`
}

func (s *Server) handleWrite(w http.ResponseWriter, req *http.Request) {
	var in writeRequest
	if err := decodeJSON(w, req, &in); err != nil {
		writeError(w, http.StatusBadRequest, "invalid JSON body: "+err.Error())
		return
	}
	if in.Key == "" {
		writeError(w, http.StatusBadRequest, "field 'key' is required")
		return
	}
	if in.Context == nil {
		in.Context = Clock{}
	}
	if err := validateTargets(in.Targets, s.cluster); err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	out := s.cluster.Write(in.Key, in.Value, in.Context, in.Coordinator, WriteOptions{
		W:       in.W,
		Targets: in.Targets,
		Timeout: time.Duration(in.TimeoutMS) * time.Millisecond,
	})
	status := http.StatusOK
	if !out.Quorum {
		status = http.StatusGatewayTimeout // 504：部分写可能已成功
	}
	writeJSON(w, status, out)
}

// readResponse 在读结果外附上集群参数，方便单条响应自解释。
type readResponse struct {
	N int `json:"n"`
	W int `json:"w"`
	R int `json:"r"`
	ReadOutcome
}

func (s *Server) handleRead(w http.ResponseWriter, req *http.Request) {
	q := req.URL.Query()
	key := q.Get("key")
	if key == "" {
		writeError(w, http.StatusBadRequest, "query parameter 'key' is required")
		return
	}
	var targets []int
	if t := q.Get("targets"); t != "" {
		ids, err := parseTargets(t)
		if err != nil {
			writeError(w, http.StatusBadRequest, err.Error())
			return
		}
		targets = ids
		if err := validateTargets(targets, s.cluster); err != nil {
			writeError(w, http.StatusBadRequest, err.Error())
			return
		}
	}
	rv, _ := strconv.Atoi(q.Get("r"))
	timeoutMS, _ := strconv.Atoi(q.Get("timeout_ms"))
	repair := q.Get("repair") == "1" || strings.EqualFold(q.Get("repair"), "true")
	full := q.Get("full") == "1" || strings.EqualFold(q.Get("full"), "true")

	out := s.cluster.Read(key, ReadOptions{
		R:          rv,
		Targets:    targets,
		Timeout:    time.Duration(timeoutMS) * time.Millisecond,
		Repair:     repair,
		RepairFull: full,
	})
	n, cw, cr := s.cluster.Config()
	resp := readResponse{N: n, W: cw, R: cr, ReadOutcome: out}
	status := http.StatusOK
	if !out.Quorum {
		status = http.StatusServiceUnavailable // 503：未凑够读仲裁；body 仍给出部分数据
	}
	writeJSON(w, status, resp)
}

// resolveRequest 通过一次因果后继写来解决冲突。
type resolveRequest struct {
	Key        string   `json:"key"`
	Value      string   `json:"value"`
	MergeAll   bool     `json:"merge_all"`   // 以全部现存兄弟版本为因果祖先
	VersionIDs []string `json:"version_ids"` // 或只合并指定版本
	W          int      `json:"w"`
	TimeoutMS  int      `json:"timeout_ms"`
}

func (s *Server) handleResolve(w http.ResponseWriter, req *http.Request) {
	var in resolveRequest
	if err := decodeJSON(w, req, &in); err != nil {
		writeError(w, http.StatusBadRequest, "invalid JSON body: "+err.Error())
		return
	}
	if in.Key == "" {
		writeError(w, http.StatusBadRequest, "field 'key' is required")
		return
	}
	// 先做一次全副本读取，拿到需要合并的兄弟版本时钟。
	cur := s.cluster.Read(in.Key, ReadOptions{Repair: false})
	ctx := Clock{}
	used := 0
	for _, v := range cur.Versions {
		use := in.MergeAll || len(in.VersionIDs) == 0
		for _, id := range in.VersionIDs {
			if id == v.ID {
				use = true
			}
		}
		if use {
			ctx.mergeInto(v.Clock)
			used++
		}
	}
	if used == 0 {
		writeError(w, http.StatusNotFound, "no matching sibling versions found to resolve; read the key first")
		return
	}
	out := s.cluster.Write(in.Key, in.Value, ctx, 0, WriteOptions{
		W:       in.W,
		Timeout: time.Duration(in.TimeoutMS) * time.Millisecond,
	})
	out.WriteID = "resolve-" + out.WriteID
	status := http.StatusOK
	if !out.Quorum {
		status = http.StatusGatewayTimeout
	}
	writeJSON(w, status, out)
}

// faultRequest 故障注入。
type faultRequest struct {
	Replica int    `json:"replica"`
	Mode    string `json:"mode"` // up | down | delay
	DelayMS int    `json:"delay_ms"`
}

func (s *Server) handleFault(w http.ResponseWriter, req *http.Request) {
	var in faultRequest
	if err := decodeJSON(w, req, &in); err != nil {
		writeError(w, http.StatusBadRequest, "invalid JSON body: "+err.Error())
		return
	}
	m := mode(in.Mode)
	if m != modeUp && m != modeDown && m != modeDelay {
		writeError(w, http.StatusBadRequest, "field 'mode' must be one of: up, down, delay")
		return
	}
	if err := s.cluster.SetFault(in.Replica, m, time.Duration(in.DelayMS)*time.Millisecond); err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"replica": in.Replica,
		"mode":    in.Mode,
		"status":  "fault applied",
	})
}

// recoverRequest 副本恢复。
type recoverRequest struct {
	Replica   int   `json:"replica"`
	From      []int `json:"from"`
	TimeoutMS int   `json:"timeout_ms"`
}

func (s *Server) handleRecover(w http.ResponseWriter, req *http.Request) {
	var in recoverRequest
	if err := decodeJSON(w, req, &in); err != nil {
		writeError(w, http.StatusBadRequest, "invalid JSON body: "+err.Error())
		return
	}
	if err := validateTargets(append([]int{in.Replica}, in.From...), s.cluster); err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	out := s.cluster.Recover(RecoverOptions{
		Target:  in.Replica,
		From:    in.From,
		Timeout: time.Duration(in.TimeoutMS) * time.Millisecond,
	})
	writeJSON(w, http.StatusOK, out)
}

func (s *Server) handleState(w http.ResponseWriter, req *http.Request) {
	n, cw, cr := s.cluster.Config()
	writeJSON(w, http.StatusOK, map[string]any{
		"n":        n,
		"w":        cw,
		"r":        cr,
		"replicas": s.cluster.Snapshot(),
	})
}

func (s *Server) handleReset(w http.ResponseWriter, req *http.Request) {
	s.cluster.Reset()
	writeJSON(w, http.StatusOK, map[string]string{"status": "reset"})
}

type configBody struct {
	N int `json:"n"`
	W int `json:"w"`
	R int `json:"r"`
}

func (s *Server) handleConfig(w http.ResponseWriter, req *http.Request) {
	if req.Method == http.MethodGet {
		n, cw, cr := s.cluster.Config()
		writeJSON(w, http.StatusOK, configBody{N: n, W: cw, R: cr})
		return
	}
	var in configBody
	if err := decodeJSON(w, req, &in); err != nil {
		writeError(w, http.StatusBadRequest, "invalid JSON body: "+err.Error())
		return
	}
	reset, err := s.cluster.SetConfig(in.N, in.W, in.R)
	if err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"n": in.N, "w": in.W, "r": in.R,
		"replicas_reset": reset,
		"status":         "config updated",
	})
}

// ---- 小工具 ----

func decodeJSON(w http.ResponseWriter, req *http.Request, v any) error {
	defer req.Body.Close()
	dec := json.NewDecoder(http.MaxBytesReader(w, req.Body, 1<<20))
	dec.DisallowUnknownFields()
	if err := dec.Decode(v); err != nil {
		return err
	}
	return nil
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	enc := json.NewEncoder(w)
	enc.SetIndent("", "  ")
	_ = enc.Encode(v)
}

func writeError(w http.ResponseWriter, status int, msg string) {
	writeJSON(w, status, map[string]any{"error": msg})
}

func parseTargets(s string) ([]int, error) {
	parts := strings.Split(s, ",")
	out := make([]int, 0, len(parts))
	for _, p := range parts {
		p = strings.TrimSpace(p)
		if p == "" {
			continue
		}
		id, err := strconv.Atoi(p)
		if err != nil {
			return nil, errors.New("invalid targets list: " + s)
		}
		out = append(out, id)
	}
	return out, nil
}

func validateTargets(ids []int, c *Cluster) error {
	n, _, _ := c.Config()
	for _, id := range ids {
		if id < 0 || id >= n {
			return errors.New("replica id out of range [0," + strconv.Itoa(n) + ")")
		}
	}
	return nil
}
