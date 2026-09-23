package kv

import (
	"strings"

	"respd/internal/resp"
)

// Session is one logical RESP client connection. HTTP is stateless, so a new
// Session is created per POST /resp request; MULTI state therefore never
// leaks between HTTP requests.
type Session struct {
	store  *Store
	queued [][]string
	inTx   bool
	// abort marks a queue-time error inside MULTI. Redis semantics: the
	// queue records the error, further commands keep queueing, and EXEC
	// refuses to run the transaction.
	abort bool
}

// NewSession creates a session bound to store.
func NewSession(store *Store) *Session {
	return &Session{store: store}
}

// Dispatch runs one parsed command vector. args[0] is the command name.
// Replies mirror Redis:
//
//	PING outside tx -> PONG; inside MULTI -> QUEUED
//	EXEC            -> array of per-command replies, or a null array when
//	                   MULTI was never issued, or an EXECABORT error
func (s *Session) Dispatch(args []string) *resp.Value {
	name := strings.ToUpper(args[0])

	if name == "MULTI" {
		if s.inTx {
			return resp.ErrVal("ERR", "MULTI calls can not be nested")
		}
		s.inTx = true
		s.abort = false
		s.queued = s.queued[:0]
		return resp.ReplyOK()
	}
	if name == "DISCARD" {
		if !s.inTx {
			return resp.ErrVal("ERR", "DISCARD without MULTI")
		}
		s.inTx = false
		s.abort = false
		s.queued = s.queued[:0]
		return resp.ReplyOK()
	}
	if name == "EXEC" {
		return s.exec()
	}

	if !s.inTx {
		// Unknown command and arity are ordinary runtime errors here.
		if _, ok := specIndex[name]; !ok {
			return resp.ErrVal("ERR", "unknown command '"+strings.ToLower(name)+"'")
		}
		if !arityOK(args) {
			return wrongArgs(strings.ToLower(name))
		}
		return s.store.exec(args)
	}

	// Inside MULTI: validate without executing.
	if _, ok := specIndex[name]; !ok {
		s.abort = true
		return resp.ErrVal("ERR", "unknown command '"+strings.ToLower(name)+"'")
	}
	if !arityOK(args) {
		s.abort = true
		return wrongArgs(strings.ToLower(name))
	}
	// Keep the vector as a copy: callers may reuse the parsed slice.
	dup := make([]string, len(args))
	copy(dup, args)
	s.queued = append(s.queued, dup)
	return resp.ReplyQueued()
}

// exec implements EXEC, including queue-time abort semantics.
func (s *Session) exec() *resp.Value {
	if !s.inTx {
		// EXEC without MULTI is a null *array*, not an error (Redis parity).
		return resp.NilArray()
	}
	queued := s.queued
	s.inTx = false
	s.queued = s.queued[:0]
	if s.abort {
		s.abort = false
		// Redis: "EXECABORT Transaction discarded because of previous errors."
		return resp.ErrVal("EXECABORT", "Transaction discarded because of previous errors.")
	}
	out := make([]*resp.Value, len(queued))
	for i, cmd := range queued {
		out[i] = s.store.exec(cmd)
	}
	return &resp.Value{Type: resp.Array, Array: out}
}
