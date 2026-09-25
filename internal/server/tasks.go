package server

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"time"

	"dynpool"
)

func parseDuration(s string) (time.Duration, error) {
	return time.ParseDuration(s)
}

// handleSubmit builds one of the built-in demo tasks.
func (s *Server) handleSubmit(w http.ResponseWriter, r *http.Request) {
	e, ok := s.get(w, r.PathValue("name"))
	if !ok {
		return
	}
	var req taskReq
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeErr(w, http.StatusBadRequest, "invalid json: %v", err)
		return
	}

	var fn dynpool.TaskFunc
	var releaseCh chan struct{}
	switch req.Type {
	case "echo":
		fn = func(context.Context) error { return nil }
	case "sleep":
		d := time.Duration(req.SleepMs) * time.Millisecond
		fn = func(ctx context.Context) error {
			select {
			case <-time.After(d):
				return nil
			case <-ctx.Done():
				return ctx.Err()
			}
		}
	case "block":
		// Block tasks must be addressable for release: require id or name.
		if req.Name == "" && req.ID == "" {
			writeErr(w, http.StatusBadRequest, "block task requires name or id")
			return
		}
		blockName := req.Name
		if blockName == "" {
			blockName = req.ID
		}
		if _, loaded := e.blocks.Load(blockName); loaded {
			writeErr(w, http.StatusConflict, "block %q already registered", blockName)
			return
		}
		releaseCh = make(chan struct{})
		e.blocks.Store(blockName, releaseCh)
		fn = func(ctx context.Context) error {
			select {
			case <-releaseCh:
				return nil
			case <-ctx.Done():
				return ctx.Err()
			}
		}
	default:
		writeErr(w, http.StatusBadRequest, "unknown task type %q (want sleep|block|echo)", req.Type)
		return
	}

	opts := []dynpool.SubmitOption{}
	if req.ID != "" {
		opts = append(opts, dynpool.WithTaskID(req.ID))
	}
	h, err := e.pool.Submit(fn, opts...)
	if err != nil {
		// Roll back the block reservation.
		if req.Type == "block" {
			name := req.Name
			if name == "" {
				name = req.ID
			}
			e.blocks.Delete(name)
		}
		code := http.StatusInternalServerError
		switch {
		case errors.Is(err, dynpool.ErrTaskRejected):
			code = http.StatusTooManyRequests
		case errors.Is(err, dynpool.ErrPoolShuttingDown), errors.Is(err, dynpool.ErrPoolStopped):
			code = http.StatusConflict
		case errors.Is(err, dynpool.ErrDuplicateTaskID), errors.Is(err, dynpool.ErrEmptyTaskID):
			code = http.StatusBadRequest
		}
		writeJSON(w, code, taskResp{
			ID:       req.ID,
			State:    "rejected",
			Rejected: true,
			Error:    err.Error(),
		})
		return
	}
	if req.Type == "echo" {
		e.results.Store(h.ID, req.Payload)
	}
	writeJSON(w, http.StatusAccepted, taskResp{ID: h.ID, State: "accepted"})
}

// handleReleaseBlock unblocks a named "block" task so it can finish. This is
// what lets the acceptance demo drive shrink/shutdown scenarios with blocking
// tasks over plain HTTP.
func (s *Server) handleReleaseBlock(w http.ResponseWriter, r *http.Request) {
	e, ok := s.get(w, r.PathValue("name"))
	if !ok {
		return
	}
	var body struct {
		Name string `json:"name"`
	}
	// Name comes from query or body.
	blockName := r.URL.Query().Get("name")
	if r.Body != nil {
		_ = json.NewDecoder(r.Body).Decode(&body)
		if body.Name != "" {
			blockName = body.Name
		}
	}
	if blockName == "" {
		writeErr(w, http.StatusBadRequest, "block name required")
		return
	}
	v, ok2 := e.blocks.LoadAndDelete(blockName)
	if !ok2 {
		writeErr(w, http.StatusNotFound, "block %q not found (already released?)", blockName)
		return
	}
	close(v.(chan struct{}))
	writeJSON(w, http.StatusOK, map[string]string{"released": blockName})
}
