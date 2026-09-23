// Package kv implements the in-memory string key space and the command
// dispatcher. It holds no network/protocol logic: commands operate on plain
// Go string arguments and return *resp.Value replies.
package kv

import (
	"errors"
	"fmt"
	"sort"
	"strconv"
	"strings"
	"sync"

	"respd/internal/resp"
)

// Store is the in-memory database. One process serves all HTTP requests;
// every command takes the lock for simplicity (the workload is a demo).
type Store struct {
	mu sync.Mutex
	db map[string]string
}

// NewStore creates an empty store.
func NewStore() *Store {
	return &Store{db: make(map[string]string)}
}

func cmdErr(format string, args ...any) *resp.Value {
	return resp.ErrVal("ERR", fmt.Sprintf(format, args...))
}

func wrongArgs(name string) *resp.Value {
	return resp.ErrVal("ERR", "wrong number of arguments for '"+name+"' command")
}

// exec runs one command against the store. args[0] is the command name.
// This is the single execution path used both for ordinary commands and
// inside a MULTI/EXEC transaction, so runtime semantics never diverge.
func (s *Store) exec(args []string) *resp.Value {
	name := strings.ToUpper(args[0])
	a := args[1:]

	switch name {
	case "PING":
		if len(a) > 1 {
			return wrongArgs("ping")
		}
		if len(a) == 1 {
			return resp.BulkStringVal(a[0])
		}
		return resp.ReplyPong()

	case "ECHO":
		if len(a) != 1 {
			return wrongArgs("echo")
		}
		return resp.BulkStringVal(a[0])

	case "SET":
		if len(a) < 2 || len(a) > 3 {
			return wrongArgs("set")
		}
		key, val := a[0], a[1]
		s.mu.Lock()
		defer s.mu.Unlock()
		if len(a) == 3 {
			opt := strings.ToUpper(a[2])
			_, exists := s.db[key]
			switch opt {
			case "NX":
				if exists {
					return resp.NilBulk()
				}
			case "XX":
				if !exists {
					return resp.NilBulk()
				}
			default:
				return cmdErr("syntax error")
			}
		}
		s.db[key] = val
		return resp.ReplyOK()

	case "GET":
		if len(a) != 1 {
			return wrongArgs("get")
		}
		s.mu.Lock()
		defer s.mu.Unlock()
		v, ok := s.db[a[0]]
		if !ok {
			return resp.NilBulk()
		}
		return resp.BulkStringVal(v)

	case "DEL":
		if len(a) < 1 {
			return wrongArgs("del")
		}
		s.mu.Lock()
		defer s.mu.Unlock()
		n := int64(0)
		for _, k := range a {
			if _, ok := s.db[k]; ok {
				delete(s.db, k)
				n++
			}
		}
		return resp.IntVal(n)

	case "INCR":
		if len(a) != 1 {
			return wrongArgs("incr")
		}
		s.mu.Lock()
		defer s.mu.Unlock()
		return s.incrBy(a[0], 1)

	case "APPEND":
		if len(a) != 2 {
			return wrongArgs("append")
		}
		s.mu.Lock()
		defer s.mu.Unlock()
		v, ok := s.db[a[0]]
		if !ok {
			v = ""
		}
		v += a[1]
		s.db[a[0]] = v
		return resp.IntVal(int64(len(v)))

	case "STRLEN":
		if len(a) != 1 {
			return wrongArgs("strlen")
		}
		s.mu.Lock()
		defer s.mu.Unlock()
		v, ok := s.db[a[0]]
		if !ok {
			return resp.IntVal(0)
		}
		return resp.IntVal(int64(len(v)))

	case "TYPE":
		if len(a) != 1 {
			return wrongArgs("type")
		}
		s.mu.Lock()
		defer s.mu.Unlock()
		if _, ok := s.db[a[0]]; !ok {
			return resp.SimpleVal("none")
		}
		return resp.SimpleVal("string")

	case "KEYS":
		if len(a) != 1 {
			return wrongArgs("keys")
		}
		pattern := a[0]
		s.mu.Lock()
		keys := make([]string, 0, len(s.db))
		for k := range s.db {
			if glob(pattern, k) {
				keys = append(keys, k)
			}
		}
		s.mu.Unlock()
		sort.Strings(keys)
		out := make([]*resp.Value, len(keys))
		for i, k := range keys {
			out[i] = resp.BulkStringVal(k)
		}
		return &resp.Value{Type: resp.Array, Array: out}

	default:
		return resp.ErrVal("ERR", "unknown command '"+strings.ToLower(name)+"'")
	}
}

// incrBy implements INCR against s.db; caller holds s.mu.
func (s *Store) incrBy(key string, delta int64) *resp.Value {
	cur, ok := s.db[key]
	n := int64(0)
	if ok {
		parsed, err := parseRedisInt(cur)
		if err != nil {
			return resp.ErrVal("ERR", "value is not an integer or out of range")
		}
		n = parsed
	}
	if (delta > 0 && n > (1<<63-1)-delta) || (delta < 0 && n < -1<<63-delta) {
		return resp.ErrVal("ERR", "increment or decrement would overflow")
	}
	n += delta
	s.db[key] = strconv.FormatInt(n, 10)
	return resp.IntVal(n)
}

// parseRedisInt parses an integer the way Redis does for string values:
// optional leading '-', decimal digits only, no '+' and no leading zeros
// ("0" is allowed).
func parseRedisInt(s string) (int64, error) {
	if s == "" {
		return 0, errNotInt
	}
	i := 0
	if s[0] == '-' {
		i = 1
		if len(s) == 1 {
			return 0, errNotInt
		}
	}
	if len(s)-i > 1 && s[i] == '0' {
		return 0, errNotInt
	}
	for ; i < len(s); i++ {
		if s[i] < '0' || s[i] > '9' {
			return 0, errNotInt
		}
	}
	n, err := strconv.ParseInt(s, 10, 64)
	if err != nil {
		return 0, errNotInt
	}
	return n, nil
}

var errNotInt = errors.New("value is not an integer")
