package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"log"
	"net/http"
	"strings"
	"time"
)

// Server 把一个 ORSet 副本暴露为 HTTP 服务。
type Server struct {
	set    *ORSet
	id     string
	client *http.Client
}

func NewServer(replicaID string) *Server {
	return &Server{
		set:    NewORSet(replicaID),
		id:     replicaID,
		client: &http.Client{Timeout: 5 * time.Second},
	}
}

func (srv *Server) routes() *http.ServeMux {
	mux := http.NewServeMux()
	mux.HandleFunc("POST /add", srv.handleAdd)
	mux.HandleFunc("POST /remove", srv.handleRemove)
	mux.HandleFunc("GET /elements", srv.handleElements)
	mux.HandleFunc("GET /state", srv.handleState)
	mux.HandleFunc("POST /merge", srv.handleMerge)
	mux.HandleFunc("POST /sync", srv.handleSync)
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, http.StatusOK, map[string]string{"status": "ok", "replica": srv.id})
	})
	return mux
}

type elementRequest struct {
	Element string `json:"element"`
}

func (srv *Server) handleAdd(w http.ResponseWriter, r *http.Request) {
	var req elementRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil || req.Element == "" {
		writeError(w, http.StatusBadRequest, "body must be JSON: {\"element\":\"...\"}")
		return
	}
	tag := srv.set.Add(req.Element)
	writeJSON(w, http.StatusOK, map[string]any{"element": req.Element, "tag": tag})
}

func (srv *Server) handleRemove(w http.ResponseWriter, r *http.Request) {
	var req elementRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil || req.Element == "" {
		writeError(w, http.StatusBadRequest, "body must be JSON: {\"element\":\"...\"}")
		return
	}
	removed := srv.set.Remove(req.Element)
	writeJSON(w, http.StatusOK, map[string]any{"element": req.Element, "removed_tags": removed})
}

func (srv *Server) handleElements(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{"elements": srv.set.Elements()})
}

func (srv *Server) handleState(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, srv.set.Snapshot())
}

// handleMerge 接收另一副本推送过来的状态并合并。合并是幂等的，
// 重复推送、乱序推送都安全。
func (srv *Server) handleMerge(w http.ResponseWriter, r *http.Request) {
	var st State
	if err := json.NewDecoder(r.Body).Decode(&st); err != nil {
		writeError(w, http.StatusBadRequest, "body must be a state JSON as returned by GET /state")
		return
	}
	srv.set.Merge(st)
	writeJSON(w, http.StatusOK, map[string]string{"status": "merged"})
}

type syncRequest struct {
	Peer string `json:"peer"` // 对端地址，如 "http://localhost:8002"
}

// handleSync 与指定对端做双向同步：先拉取对端状态合并到本地，
// 再把合并后的本地状态推回对端。完成后两个副本状态一致。
func (srv *Server) handleSync(w http.ResponseWriter, r *http.Request) {
	var req syncRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil || req.Peer == "" {
		writeError(w, http.StatusBadRequest, "body must be JSON: {\"peer\":\"http://host:port\"}")
		return
	}
	peer := strings.TrimRight(req.Peer, "/")

	// 1. 拉取对端状态
	resp, err := srv.client.Get(peer + "/state")
	if err != nil {
		writeError(w, http.StatusBadGateway, "fetch peer state: "+err.Error())
		return
	}
	defer resp.Body.Close()
	var peerState State
	if err := json.NewDecoder(resp.Body).Decode(&peerState); err != nil {
		writeError(w, http.StatusBadGateway, "decode peer state: "+err.Error())
		return
	}

	// 2. 合并到本地
	srv.set.Merge(peerState)

	// 3. 把合并后的状态推回对端（对端合并是幂等的，重复推送无害）
	pushResp, err := srv.client.Post(peer+"/merge", "application/json",
		strings.NewReader(mustJSON(srv.set.Snapshot())))
	if err != nil {
		writeError(w, http.StatusBadGateway, "push state to peer: "+err.Error())
		return
	}
	pushResp.Body.Close()

	writeJSON(w, http.StatusOK, map[string]string{"status": "synced", "peer": peer})
}

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	json.NewEncoder(w).Encode(v)
}

func writeError(w http.ResponseWriter, code int, msg string) {
	writeJSON(w, code, map[string]string{"error": msg})
}

func mustJSON(v any) string {
	b, err := json.Marshal(v)
	if err != nil {
		panic(err)
	}
	return string(b)
}

func main() {
	id := flag.String("id", "replica-1", "replica ID (used to generate unique add tags)")
	addr := flag.String("addr", ":8001", "listen address")
	flag.Parse()

	srv := NewServer(*id)
	fmt.Printf("OR-Set replica %q listening on %s\n", *id, *addr)
	log.Fatal(http.ListenAndServe(*addr, srv.routes()))
}
