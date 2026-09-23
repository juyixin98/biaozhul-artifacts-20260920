// Package kv is the in-memory, thread-safe string store behind the RESP
// server. It models 16 numbered databases like Redis SELECT 0..15, supports
// millisecond TTLs with lazy expiry, and copies every byte crossing its
// boundary so callers cannot mutate stored data in place.
package kv

import (
	"errors"
	"strconv"
	"sync"
	"time"
)

// DBCount is the fixed number of selectable databases.
const DBCount = 16

var (
	// ErrDBIndex is returned for a SELECT index outside [0, DBCount).
	ErrDBIndex = errors.New("ERR DB index is out of range")
	// ErrNotInteger is returned by INCR/DECR on non-numeric values.
	ErrNotInteger = errors.New("ERR value is not an integer or out of range")
	// ErrOverflow is returned when an increment would leave int64.
	ErrOverflow = errors.New("ERR increment or decrement would overflow")
)

// entry is one stored string plus its optional absolute expiry time.
type entry struct {
	value []byte
	// expireAt is zero for persistent keys; otherwise an absolute
	// Unix-milli timestamp.
	expireAt int64
}

// Store holds every database.
type Store struct {
	mu  sync.Mutex
	dbs [DBCount]map[string]*entry

	// now is overridable in tests; production leaves it nil.
	now func() int64
}

// New returns an empty store.
func New() *Store {
	s := &Store{now: nil}
	for i := range s.dbs {
		s.dbs[i] = make(map[string]*entry)
	}
	return s
}

func (s *Store) clock() int64 {
	if s.now != nil {
		return s.now()
	}
	return time.Now().UnixMilli()
}

// ClockMs exposes the store's current millisecond clock (used by the
// server for absolute SET EX deadlines).
func (s *Store) ClockMs() int64 { return s.clock() }

// SetClock overrides the clock; test helper only.
func (s *Store) SetClock(f func() int64) { s.now = f }

// TTL returns the remaining milliseconds of key. hasKey is false when the
// key does not exist; hasTTL is false when it exists without an expiry.
func (s *Store) TTL(db int, key string, nowMs int64) (ms int64, hasKey bool, hasTTL bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	e, ok := s.alive(db, key)
	if !ok {
		return 0, false, false
	}
	if e.expireAt == 0 {
		return 0, true, false
	}
	return e.expireAt - nowMs, true, true
}

func copyBytes(b []byte) []byte {
	cp := make([]byte, len(b))
	copy(cp, b)
	return cp
}

// alive deletes and reports false for a present-but-expired entry.
func (s *Store) alive(db int, key string) (*entry, bool) {
	e, ok := s.dbs[db][key]
	if !ok {
		return nil, false
	}
	if e.expireAt != 0 && e.expireAt <= s.clock() {
		delete(s.dbs[db], key)
		return nil, false
	}
	return e, true
}

// Get returns a copy of the value and true, or nil/false when absent.
// The caller may retain the returned slice.
func (s *Store) Get(db int, key string) ([]byte, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	e, ok := s.alive(db, key)
	if !ok {
		return nil, false
	}
	return copyBytes(e.value), true
}

// SetOptions controls Set.
type SetOptions struct {
	// NX: set only if the key does not exist; XX: only if it exists.
	NX, XX bool
	// KeepTTL preserves an existing expiry.
	KeepTTL bool
	// ExpireAtMs, when non-zero, sets an absolute Unix-milli expiry.
	ExpireAtMs int64
}

// Set stores value under key. It returns false when an NX/XX precondition
// blocks the write. Any pre-existing TTL is cleared unless KeepTTL is set.
func (s *Store) Set(db int, key string, value []byte, opts SetOptions) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	old, exists := s.alive(db, key)
	if opts.NX && exists {
		return false
	}
	if opts.XX && !exists {
		return false
	}
	e := &entry{value: copyBytes(value)}
	if opts.KeepTTL && exists {
		e.expireAt = old.expireAt
	} else {
		e.expireAt = opts.ExpireAtMs
	}
	s.dbs[db][key] = e
	return true
}

// MSet writes every key/value pair atomically.
func (s *Store) MSet(db int, pairs [][]byte) {
	s.mu.Lock()
	defer s.mu.Unlock()
	for i := 0; i+1 < len(pairs); i += 2 {
		s.dbs[db][string(pairs[i])] = &entry{value: copyBytes(pairs[i+1])}
	}
}

// GetSet stores value and returns the previous value (nil when the key was
// absent). TTL is cleared, matching Redis GETSET semantics.
func (s *Store) GetSet(db int, key string, value []byte) []byte {
	s.mu.Lock()
	defer s.mu.Unlock()
	old, existed := s.alive(db, key)
	var prev []byte
	if existed {
		prev = copyBytes(old.value)
	}
	s.dbs[db][key] = &entry{value: copyBytes(value)}
	return prev
}

// Append appends value to key, creating it if missing. Returns new length.
func (s *Store) Append(db int, key string, value []byte) int {
	s.mu.Lock()
	defer s.mu.Unlock()
	e, ok := s.alive(db, key)
	if !ok {
		s.dbs[db][key] = &entry{value: copyBytes(value)}
		return len(value)
	}
	merged := make([]byte, 0, len(e.value)+len(value))
	merged = append(merged, e.value...)
	merged = append(merged, value...)
	e.value = merged
	return len(merged)
}

// StrLen returns the byte length of key or 0 when absent.
func (s *Store) StrLen(db int, key string) int {
	s.mu.Lock()
	defer s.mu.Unlock()
	e, ok := s.alive(db, key)
	if !ok {
		return 0
	}
	return len(e.value)
}

// Incr adds delta to the integer stored at key, creating it as 0 first. It
// applies the same grammar Redis uses: optional '-' then digits only.
func (s *Store) Incr(db int, key string, delta int64) (int64, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	e, ok := s.alive(db, key)
	var cur int64
	if ok {
		n, err := parseRedisInt(e.value)
		if err != nil {
			return 0, ErrNotInteger
		}
		cur = n
	}
	switch {
	case delta > 0 && cur > (1<<63-1)-delta:
		return 0, ErrOverflow
	case delta < 0 && cur < -(1<<63)-delta:
		return 0, ErrOverflow
	}
	cur += delta
	s.dbs[db][key] = &entry{value: []byte(strconv.FormatInt(cur, 10))}
	return cur, nil
}

// parseRedisInt accepts an optional leading '-' followed by ASCII digits.
// Unlike strconv.ParseInt it rejects '+' signs, spaces and surrounding
// whitespace.
func parseRedisInt(b []byte) (int64, error) {
	if len(b) == 0 {
		return 0, ErrNotInteger
	}
	neg := false
	i := 0
	if b[0] == '-' {
		neg = true
		i = 1
		if len(b) == 1 {
			return 0, ErrNotInteger
		}
	}
	var n uint64
	limit := uint64(1<<63 - 1)
	if neg {
		limit = 1 << 63
	}
	for ; i < len(b); i++ {
		c := b[i]
		if c < '0' || c > '9' {
			return 0, ErrNotInteger
		}
		d := uint64(c - '0')
		if n > (limit-d)/10 {
			return 0, ErrNotInteger
		}
		n = n*10 + d
	}
	if neg {
		return -int64(n), nil
	}
	return int64(n), nil
}

// Exists reports how many of keys are present.
func (s *Store) Exists(db int, keys []string) int {
	s.mu.Lock()
	defer s.mu.Unlock()
	n := 0
	for _, k := range keys {
		if _, ok := s.alive(db, k); ok {
			n++
		}
	}
	return n
}

// Del removes keys and returns how many were present.
func (s *Store) Del(db int, keys []string) int {
	s.mu.Lock()
	defer s.mu.Unlock()
	n := 0
	for _, k := range keys {
		if _, ok := s.alive(db, k); ok {
			delete(s.dbs[db], k)
			n++
		}
	}
	return n
}

// Keys returns every live key in db matching pattern (Redis glob syntax).
func (s *Store) Keys(db int, pattern string) []string {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make([]string, 0)
	for k := range s.dbs[db] {
		if _, ok := s.alive(db, k); ok && globMatch(pattern, k) {
			out = append(out, k)
		}
	}
	return out
}

// DBSize returns the live key count of db.
func (s *Store) DBSize(db int) int {
	s.mu.Lock()
	defer s.mu.Unlock()
	n := 0
	for k := range s.dbs[db] {
		if _, ok := s.alive(db, k); ok {
			n++
		}
	}
	return n
}

// FlushDB removes every key in db.
func (s *Store) FlushDB(db int) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.dbs[db] = make(map[string]*entry)
}

// SetTTL attaches an absolute Unix-milli deadline; it returns false if the
// key is absent.
func (s *Store) SetTTL(db int, key string, expireAtMs int64) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	e, ok := s.alive(db, key)
	if !ok {
		return false
	}
	e.expireAt = expireAtMs
	return true
}

// Persist removes the TTL of key; false if the key is absent or already
// persistent.
func (s *Store) Persist(db int, key string) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	e, ok := s.alive(db, key)
	if !ok || e.expireAt == 0 {
		return false
	}
	e.expireAt = 0
	return true
}

// globMatch implements the Redis KEYS wildcard grammar: '*' matches any run
// of bytes, '?' exactly one byte, '[...]' a character class including
// negation ([^...]) and ranges, and '\\' escapes the next byte.
func globMatch(pattern, name string) bool {
	return globRec([]byte(pattern), []byte(name))
}

func globRec(p, s []byte) bool {
	for len(p) > 0 {
		switch p[0] {
		case '*':
			// Collapse consecutive stars.
			for len(p) > 1 && p[1] == '*' {
				p = p[1:]
			}
			if len(p) == 1 {
				return true
			}
			for i := 0; i <= len(s); i++ {
				if globRec(p[1:], s[i:]) {
					return true
				}
			}
			return false
		case '?':
			if len(s) == 0 {
				return false
			}
			s = s[1:]
			p = p[1:]
		case '[':
			if len(s) == 0 {
				return false
			}
			consumed, ok := matchClass(p, s[0])
			if !ok {
				return false
			}
			p = p[consumed:]
			s = s[1:]
		case '\\':
			if len(p) < 2 {
				return len(s) > 0 && s[0] == '\\'
			}
			if len(s) == 0 || s[0] != p[1] {
				return false
			}
			p = p[2:]
			s = s[1:]
		default:
			if len(s) == 0 || s[0] != p[0] {
				return false
			}
			p = p[1:]
			s = s[1:]
		}
	}
	return len(s) == 0
}

// matchClass evaluates a [...] class starting at p[0] against byte c and
// returns the number of pattern bytes the class occupies.
func matchClass(p []byte, c byte) (int, bool) {
	// p starts with '['.
	i := 1
	negate := false
	if i < len(p) && (p[i] == '^' || p[i] == ']') {
		// In Redis glob, a leading ']' is a literal; '^' negates.
		if p[i] == '^' {
			negate = true
			i++
		}
	}
	matched := false
	for i < len(p) && p[i] != ']' {
		lo := p[i]
		if lo == '\\' && i+1 < len(p) {
			i++
			lo = p[i]
		}
		i++
		if i+1 < len(p) && p[i] == '-' && p[i+1] != ']' {
			hi := p[i+1]
			if hi == '\\' && i+2 < len(p) {
				i++
				hi = p[i+1]
			}
			if c >= lo && c <= hi {
				matched = true
			}
			i += 2
		} else if c == lo {
			matched = true
		}
	}
	if i >= len(p) {
		// Unterminated class: treat '[' as a literal.
		return 1, c == '['
	}
	if negate {
		matched = !matched
	}
	return i + 1, matched
}

// PatternAll is the glob matching every key ("*").
const PatternAll = "*"
