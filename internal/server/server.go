// Package server wires the RESP2 codec and the key/value store to an
// net/http surface. Commands execute under a single global lock, mirroring
// the command-serialization semantics of a single Redis connection, so a
// queued MULTI/EXEC block always runs as one uninterrupted batch.
package server

import (
	"io"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"respd/internal/kv"
	"respd/internal/resp"
)

// DefaultIdleTTL is how long a named session survives without a request.
const DefaultIdleTTL = 5 * time.Minute

// session is one client's transactional state and selected database.
type session struct {
	mu sync.Mutex

	db int

	multi bool
	// dirty records a queue-time failure (unknown command, bad arity,
	// malformed frame); EXEC then aborts without running anything.
	dirty       bool
	abortReason string
	queue       []resp.Value

	lastUsed atomic.Int64 // Unix milliseconds
}

func newSession() *session {
	s := &session{}
	s.touch()
	return s
}

func (s *session) touch() { s.lastUsed.Store(time.Now().UnixMilli()) }

// resetLocked clears transaction state; caller holds mu.
func (s *session) resetLocked() {
	s.multi = false
	s.dirty = false
	s.queue = nil
}

// Server holds the store, session table and protocol configuration.
type Server struct {
	store *kv.Store

	// execMu serializes every command, exactly like Redis's single command
	// loop, which gives EXEC atomicity for free.
	execMu sync.Mutex

	sessMu   sync.Mutex
	sessions map[string]*session

	idleTTL time.Duration
	maxBody int64
	limits  resp.Limits

	stopCh chan struct{}
	once   sync.Once
}

// Option configures a Server.
type Option func(*Server)

// WithIdleTTL sets the named-session idle timeout.
func WithIdleTTL(d time.Duration) Option {
	return func(s *Server) { s.idleTTL = d }
}

// WithMaxBody caps a single HTTP request body.
func WithMaxBody(n int64) Option {
	return func(s *Server) { s.maxBody = n }
}

// WithLimits overrides RESP decoder limits.
func WithLimits(l resp.Limits) Option {
	return func(s *Server) { s.limits = l }
}

// New constructs a server over store and launches the idle-session sweeper.
func New(store *kv.Store, opts ...Option) *Server {
	s := &Server{
		store:    store,
		sessions: make(map[string]*session),
		idleTTL:  DefaultIdleTTL,
		maxBody:  128 * 1024 * 1024,
		limits:   resp.DefaultLimits(),
		stopCh:   make(chan struct{}),
	}
	for _, o := range opts {
		o(s)
	}
	go s.sweepLoop()
	return s
}

// Close stops the background sweeper.
func (s *Server) Close() {
	s.once.Do(func() { close(s.stopCh) })
}

func (s *Server) sweepLoop() {
	t := time.NewTicker(time.Minute)
	defer t.Stop()
	for {
		select {
		case <-s.stopCh:
			return
		case now := <-t.C:
			s.sweep(now.UnixMilli())
		}
	}
}

func (s *Server) sweep(nowMs int64) {
	s.sessMu.Lock()
	defer s.sessMu.Unlock()
	for id, sess := range s.sessions {
		// Never evict a session currently processing a request.
		if !sess.mu.TryLock() {
			continue
		}
		idle := nowMs-sess.lastUsed.Load() >= s.idleTTL.Milliseconds()
		sess.mu.Unlock()
		if idle {
			delete(s.sessions, id)
		}
	}
}

// sessionFor returns the named session, creating it on first use. An empty
// id yields an ephemeral session scoped to this request (MULTI/EXEC inside
// one pipelined body still works).
func (s *Server) sessionFor(id string) *session {
	if id == "" {
		return newSession()
	}
	s.sessMu.Lock()
	defer s.sessMu.Unlock()
	sess, ok := s.sessions[id]
	if !ok {
		sess = newSession()
		s.sessions[id] = sess
	}
	sess.touch()
	return sess
}

// process drains every complete command from dec and returns one reply per
// command in order. A decode error ends the loop; callers append the
// encoded error reply to the stream themselves.
func (s *Server) process(sess *session, dec *resp.Decoder) ([]resp.Value, error) {
	var replies []resp.Value
	for {
		v, err := dec.Next()
		if err == io.EOF {
			return replies, nil
		}
		if err != nil {
			return replies, err
		}
		replies = append(replies, s.dispatch(sess, v)...)
		sess.touch()
	}
}

// dispatch executes one decoded command array and returns its replies.
// Exactly one reply is produced for every accepted command except EXEC,
// which also yields one reply (an array).
func (s *Server) dispatch(sess *session, v resp.Value) []resp.Value {
	return s.dispatchLocked(sess, v, true)
}

// dispatchLocked runs one command; lock=false when the caller already
// holds sess.mu (HTTP handlers hold it across the whole pipeline so that
// concurrent requests on one session cannot interleave).
func (s *Server) dispatchLocked(sess *session, v resp.Value, lock bool) []resp.Value {
	if lock {
		sess.mu.Lock()
		defer sess.mu.Unlock()
	}

	name, args, protoMsg := commandParts(v)
	if protoMsg != "" {
		// A malformed command frame in a transaction poisons it the way a
		// network protocol error does in Redis.
		if sess.multi {
			sess.resetLocked()
		}
		return []resp.Value{resp.SimpleError(protoMsg)}
	}
	upper := strings.ToUpper(name)

	if !sess.multi {
		switch upper {
		case "MULTI":
			if len(args) != 0 {
				return []resp.Value{wrongArgs(name)}
			}
			sess.multi = true
			return []resp.Value{resp.SimpleString("OK")}
		case "EXEC":
			return []resp.Value{resp.SimpleError("ERR EXEC without MULTI")}
		case "DISCARD":
			return []resp.Value{resp.SimpleError("ERR DISCARD without MULTI")}
		}
		return []resp.Value{s.runOne(sess, name, args)}
	}

	// Inside MULTI: only the transaction control commands run immediately;
	// everything else is validated and queued.
	switch upper {
	case "EXEC":
		if len(args) != 0 {
			return []resp.Value{wrongArgs(name)}
		}
		if sess.dirty {
			sess.resetLocked()
			return []resp.Value{execAbort(sess)}
		}
		queued := sess.queue
		sess.resetLocked()
		outs := make([]resp.Value, 0, len(queued))
		s.execMu.Lock()
		for _, qv := range queued {
			qn, qa, _ := commandParts(qv) // validated while queueing
			outs = append(outs, s.runOneLocked(sess, qn, qa))
		}
		s.execMu.Unlock()
		return []resp.Value{resp.ArrayValue(outs...)}
	case "DISCARD":
		if len(args) != 0 {
			return []resp.Value{wrongArgs(name)}
		}
		sess.resetLocked()
		return []resp.Value{resp.SimpleString("OK")}
	case "MULTI":
		// Redis reports nesting but does not poison the transaction.
		return []resp.Value{resp.SimpleError("ERR MULTI calls can not be nested")}
	default:
		spec, ok := commands[upper]
		if !ok {
			sess.dirty = true
			sess.abortReason = unknownCommand(name).Str
			return []resp.Value{unknownCommand(name)}
		}
		if !arityOK(spec.arity, len(args)+1) {
			sess.dirty = true
			wa := wrongArgs(name)
			sess.abortReason = wa.Str
			return []resp.Value{wa}
		}
		sess.queue = append(sess.queue, v)
		return []resp.Value{resp.SimpleString("QUEUED")}
	}
}

// execAbort renders the Redis-style EXECABORT error listing the first
// queue-time failure recorded on the session.
func execAbort(sess *session) resp.Value {
	msg := sess.abortReason
	if msg == "" {
		msg = "previous errors"
	}
	return resp.SimpleError("EXECABORT Transaction discarded because of: " + msg)
}

// runOne validates and executes a single command outside the queue path.
func (s *Server) runOne(sess *session, name string, args [][]byte) resp.Value {
	spec, ok := commands[strings.ToUpper(name)]
	if !ok {
		return unknownCommand(name)
	}
	if !arityOK(spec.arity, len(args)+1) {
		return wrongArgs(name)
	}
	s.execMu.Lock()
	defer s.execMu.Unlock()
	return s.runOneLocked(sess, name, args)
}

func (s *Server) runOneLocked(sess *session, name string, args [][]byte) resp.Value {
	spec := commands[strings.ToUpper(name)]
	return spec.fn(s, sess, args)
}

// commandParts flattens a command array to its name and argument bytes. The
// returned message is empty on success, otherwise an "ERR Protocol error"
// description.
func commandParts(v resp.Value) (string, [][]byte, string) {
	if v.Kind != resp.KindArray || v.Array == nil {
		return "", nil, "ERR Protocol error: expected multibulk command"
	}
	if len(v.Array) == 0 {
		return "", nil, "ERR Protocol error: empty command array"
	}
	parts := make([][]byte, len(v.Array))
	for i, el := range v.Array {
		if el.Kind != resp.KindBulk || el.Bulk == nil {
			return "", nil, "ERR Protocol error: invalid multibulk processing, expected bulk in command array"
		}
		parts[i] = el.Bulk
	}
	return string(parts[0]), parts[1:], ""
}

func arityOK(arity, got int) bool {
	if arity > 0 {
		return arity == got
	}
	return got >= -arity
}

func unknownCommand(name string) resp.Value {
	return resp.SimpleError("ERR unknown command '" + name + "'")
}

func wrongArgs(name string) resp.Value {
	return resp.SimpleError("ERR wrong number of arguments for '" +
		strings.ToLower(name) + "' command")
}

func syntaxErr() resp.Value {
	return resp.SimpleError("ERR syntax error")
}
