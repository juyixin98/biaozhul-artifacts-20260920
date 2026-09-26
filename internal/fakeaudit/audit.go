// Package fakeaudit 是一个跑在本进程里的"假外部系统"。
//
// 它通过真实的 loopback HTTP 暴露接口（不是函数直调），因此能逼真地模拟：
// 网络成功、网络失败、以及"服务端已处理但响应在回程丢失"。它还会统计自己
// 实际被写入的次数——这个计数用于在验收中明确展示本项目的边界：
//
//	幂等层只保证【本地事务副作用】恰好一次；
//	任意外部系统调用不在本地事务内，可能被执行多次。
package fakeaudit

import (
	"encoding/json"
	"fmt"
	"net/http"
	"sync"
	"time"
)

// Event 是收到的一条审计事件。
type Event struct {
	TxID    string    `json:"tx_id"`
	Account string    `json:"account"`
	Amount  int64     `json:"amount"`
	IdemKey string    `json:"idem_key"`
	Attempt uint64    `json:"attempt"`
	At      time.Time `json:"at"`
}

// Server 是假审计系统。
type Server struct {
	mu         sync.Mutex
	events     []Event
	failN      int // 还需失败多少次（不记录事件）
	failStoreN int // 还需"先记录事件再返回失败"多少次（模拟响应回程丢失）
	hangFor    time.Duration
	calls      int
}

// New 创建假审计服务。
func New() *Server { return &Server{} }

// FailNextN 让接下来 n 次写入请求返回 503 且不记录事件（服务在处理前失败）。
func (s *Server) FailNextN(n int) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.failN = n
}

// RecordThenFailNextN 让接下来 n 次请求【先写入事件，再返回 503】。
// 这模拟"外部系统实际已处理，但响应在回程中丢失"——调用方无法分辨，
// 重试会导致外部系统出现两条事件。
func (s *Server) RecordThenFailNextN(n int) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.failStoreN = n
}

// Handler 返回应挂载到 http mux 上的处理器（路径 /audit/events）。
func (s *Server) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/audit/events", s.handle)
	mux.HandleFunc("/audit/reset", s.handleReset)
	mux.HandleFunc("/audit/inspect", s.handleInspect)
	mux.HandleFunc("/audit/faults", s.handleFaults)
	return mux
}

// faultRequest 是测试控制面：注入下几次调用的故障行为。
type faultRequest struct {
	FailNextN           *int `json:"fail_next_n,omitempty"`
	RecordThenFailNextN *int `json:"record_then_fail_next_n,omitempty"`
	HangMS              *int `json:"hang_ms,omitempty"`
}

// HangNextFor 让下一次写入请求等待 d 后才响应（模拟处理慢/回程延迟）。
func (s *Server) HangNextFor(d time.Duration) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.hangFor = d
}

// EventCount 返回审计系统实际收到的事件数。
func (s *Server) EventCount() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return len(s.events)
}

// Calls 返回写入接口被调用的总次数（含失败的）。
func (s *Server) Calls() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.calls
}

// Events 返回已接收事件的副本。
func (s *Server) Events() []Event {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make([]Event, len(s.events))
	copy(out, s.events)
	return out
}

// Reset 清空全部事件与故障注入状态（进程内调用）。
func (s *Server) Reset() {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.events = nil
	s.failN = 0
	s.failStoreN = 0
	s.hangFor = 0
	s.calls = 0
}

func (s *Server) handle(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	var e Event
	if err := json.NewDecoder(r.Body).Decode(&e); err != nil {
		http.Error(w, "bad request: "+err.Error(), http.StatusBadRequest)
		return
	}

	s.mu.Lock()
	s.calls++
	hang := s.hangFor
	if hang > 0 {
		s.hangFor = 0
	}
	fail := s.failN > 0
	if fail {
		s.failN--
	}
	recordThenFail := s.failStoreN > 0
	if recordThenFail {
		s.failStoreN--
	}
	s.mu.Unlock()

	if hang > 0 {
		select {
		case <-time.After(hang):
		case <-r.Context().Done():
			return // 客户端先断开
		}
	}
	if fail {
		http.Error(w, `{"error":"audit system temporarily unavailable"}`, http.StatusServiceUnavailable)
		return
	}

	s.mu.Lock()
	s.events = append(s.events, e)
	count := len(s.events)
	s.mu.Unlock()

	if recordThenFail {
		// 事件已落账，但对调用方表现为失败：调用方重试时外部系统会再收到一次。
		http.Error(w, `{"error":"response lost after persist"}`, http.StatusServiceUnavailable)
		return
	}

	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusCreated)
	_, _ = fmt.Fprintf(w, `{"ok":true,"audit_seq":%d}`, count)
}

func (s *Server) handleReset(w http.ResponseWriter, _ *http.Request) {
	s.mu.Lock()
	s.events = nil
	s.failN = 0
	s.failStoreN = 0
	s.hangFor = 0
	s.calls = 0
	s.mu.Unlock()
	w.Header().Set("Content-Type", "application/json")
	_, _ = w.Write([]byte(`{"ok":true}`))
}

// handleFaults 是测试控制面：POST JSON 设置接下来调用的故障。
func (s *Server) handleFaults(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	var fr faultRequest
	if err := json.NewDecoder(r.Body).Decode(&fr); err != nil {
		http.Error(w, "bad request: "+err.Error(), http.StatusBadRequest)
		return
	}
	s.mu.Lock()
	if fr.FailNextN != nil {
		s.failN = *fr.FailNextN
	}
	if fr.RecordThenFailNextN != nil {
		s.failStoreN = *fr.RecordThenFailNextN
	}
	if fr.HangMS != nil {
		s.hangFor = time.Duration(*fr.HangMS) * time.Millisecond
	}
	s.mu.Unlock()
	w.Header().Set("Content-Type", "application/json")
	_, _ = w.Write([]byte(`{"ok":true}`))
}

func (s *Server) handleInspect(w http.ResponseWriter, _ *http.Request) {
	s.mu.Lock()
	ev := make([]Event, len(s.events))
	copy(ev, s.events)
	resp := map[string]any{"calls": s.calls, "events": ev}
	s.mu.Unlock()
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(resp)
}
