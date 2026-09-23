package server

import (
	"strconv"
	"strings"

	"respd/internal/kv"
	"respd/internal/resp"
)

type commandSpec struct {
	// arity follows the Redis convention: a positive number is the exact
	// argc (including command name); a negative number -n means "at least
	// n arguments total".
	arity int
	fn    func(s *Server, sess *session, args [][]byte) resp.Value
}

// commands is the supported RESP2 command subset. Keys are uppercase.
var commands = map[string]commandSpec{
	"PING":    {arity: -1, fn: ping},
	"ECHO":    {arity: 2, fn: echo},
	"GET":     {arity: 2, fn: get},
	"SET":     {arity: -3, fn: set},
	"GETSET":  {arity: 3, fn: getset},
	"MSET":    {arity: -3, fn: mset},
	"MGET":    {arity: -2, fn: mget},
	"APPEND":  {arity: 3, fn: appendCmd},
	"STRLEN":  {arity: 2, fn: strlen},
	"INCR":    {arity: 2, fn: incr},
	"INCRBY":  {arity: 3, fn: incrby},
	"DECR":    {arity: 2, fn: decr},
	"DECRBY":  {arity: 3, fn: decrby},
	"EXISTS":  {arity: -2, fn: exists},
	"DEL":     {arity: -2, fn: del},
	"TYPE":    {arity: 2, fn: typeCmd},
	"KEYS":    {arity: 2, fn: keys},
	"DBSIZE":  {arity: 1, fn: dbsize},
	"FLUSHDB": {arity: -1, fn: flushdb},
	"SELECT":  {arity: 2, fn: selectDB},
	"EXPIRE":  {arity: 3, fn: expire},
	"PEXPIRE": {arity: 3, fn: pexpire},
	"TTL":     {arity: 2, fn: ttl},
	"PTTL":    {arity: 2, fn: pttl},
	"PERSIST": {arity: 2, fn: persist},
	"COMMAND": {arity: -1, fn: commandCmd},
}

func ping(s *Server, _ *session, args [][]byte) resp.Value {
	if len(args) == 0 {
		return resp.SimpleString("PONG")
	}
	return resp.BulkBytes(args[0])
}

func echo(s *Server, _ *session, args [][]byte) resp.Value {
	return resp.BulkBytes(args[0])
}

func get(s *Server, sess *session, args [][]byte) resp.Value {
	v, ok := s.store.Get(sess.db, string(args[0]))
	if !ok {
		return resp.NullBulk()
	}
	return resp.BulkBytes(v)
}

func set(s *Server, sess *session, args [][]byte) resp.Value {
	key := string(args[0])
	value := args[1]
	opts := kv.SetOptions{}
	for i := 2; i < len(args); i++ {
		switch strings.ToUpper(string(args[i])) {
		case "NX":
			opts.NX = true
		case "XX":
			opts.XX = true
		case "KEEPTTL":
			opts.KeepTTL = true
		case "EX", "PX":
			if i+1 >= len(args) {
				return syntaxErr()
			}
			n, err := strconv.ParseInt(string(args[i+1]), 10, 64)
			if err != nil || n <= 0 {
				return resp.SimpleError("ERR value is not an integer or out of range")
			}
			ms := n
			if strings.EqualFold(string(args[i]), "EX") {
				ms = n * 1000
			}
			opts.ExpireAtMs = s.store.ClockMs() + ms
			i++
		default:
			return syntaxErr()
		}
		if opts.NX && opts.XX {
			return syntaxErr()
		}
	}
	if s.store.Set(sess.db, key, value, opts) {
		return resp.SimpleString("OK")
	}
	return resp.NullBulk()
}

func getset(s *Server, sess *session, args [][]byte) resp.Value {
	old := s.store.GetSet(sess.db, string(args[0]), args[1])
	if old == nil {
		return resp.NullBulk()
	}
	return resp.BulkBytes(old)
}

func mset(s *Server, sess *session, args [][]byte) resp.Value {
	if len(args)%2 != 0 {
		return syntaxErr()
	}
	s.store.MSet(sess.db, args)
	return resp.SimpleString("OK")
}

func mget(s *Server, sess *session, args [][]byte) resp.Value {
	out := make([]resp.Value, len(args))
	for i, k := range args {
		v, ok := s.store.Get(sess.db, string(k))
		if !ok {
			out[i] = resp.NullBulk()
		} else {
			out[i] = resp.BulkBytes(v)
		}
	}
	return resp.ArrayValue(out...)
}

func appendCmd(s *Server, sess *session, args [][]byte) resp.Value {
	n := s.store.Append(sess.db, string(args[0]), args[1])
	return resp.Integer(int64(n))
}

func strlen(s *Server, sess *session, args [][]byte) resp.Value {
	return resp.Integer(int64(s.store.StrLen(sess.db, string(args[0]))))
}

func incr(s *Server, sess *session, args [][]byte) resp.Value {
	return doIncr(s, sess, args[0], 1)
}

func decr(s *Server, sess *session, args [][]byte) resp.Value {
	return doIncr(s, sess, args[0], -1)
}

func incrby(s *Server, sess *session, args [][]byte) resp.Value {
	n, err := strconv.ParseInt(string(args[1]), 10, 64)
	if err != nil {
		return resp.SimpleError("ERR value is not an integer or out of range")
	}
	return doIncr(s, sess, args[0], n)
}

func decrby(s *Server, sess *session, args [][]byte) resp.Value {
	n, err := strconv.ParseInt(string(args[1]), 10, 64)
	if err != nil {
		return resp.SimpleError("ERR value is not an integer or out of range")
	}
	return doIncr(s, sess, args[0], -n)
}

func doIncr(s *Server, sess *session, key []byte, delta int64) resp.Value {
	n, err := s.store.Incr(sess.db, string(key), delta)
	if err != nil {
		return resp.SimpleError(err.Error())
	}
	return resp.Integer(n)
}

func exists(s *Server, sess *session, args [][]byte) resp.Value {
	keys := make([]string, len(args))
	for i, a := range args {
		keys[i] = string(a)
	}
	return resp.Integer(int64(s.store.Exists(sess.db, keys)))
}

func del(s *Server, sess *session, args [][]byte) resp.Value {
	keys := make([]string, len(args))
	for i, a := range args {
		keys[i] = string(a)
	}
	return resp.Integer(int64(s.store.Del(sess.db, keys)))
}

func typeCmd(s *Server, sess *session, args [][]byte) resp.Value {
	if _, ok := s.store.Get(sess.db, string(args[0])); ok {
		return resp.SimpleString("string")
	}
	return resp.SimpleString("none")
}

func keys(s *Server, sess *session, args [][]byte) resp.Value {
	found := s.store.Keys(sess.db, string(args[0]))
	out := make([]resp.Value, len(found))
	for i, k := range found {
		out[i] = resp.BulkString(k)
	}
	return resp.ArrayValue(out...)
}

func dbsize(s *Server, sess *session, _ [][]byte) resp.Value {
	return resp.Integer(int64(s.store.DBSize(sess.db)))
}

func flushdb(s *Server, sess *session, args [][]byte) resp.Value {
	if len(args) > 0 && !strings.EqualFold(string(args[0]), "SYNC") &&
		!strings.EqualFold(string(args[0]), "ASYNC") {
		return syntaxErr()
	}
	s.store.FlushDB(sess.db)
	return resp.SimpleString("OK")
}

func selectDB(s *Server, sess *session, args [][]byte) resp.Value {
	n, err := strconv.Atoi(string(args[0]))
	if err != nil || n < 0 || n >= kv.DBCount {
		return resp.SimpleError(kv.ErrDBIndex.Error())
	}
	sess.db = n
	return resp.SimpleString("OK")
}

func expire(s *Server, sess *session, args [][]byte) resp.Value {
	return doExpire(s, sess, args, true)
}

func pexpire(s *Server, sess *session, args [][]byte) resp.Value {
	return doExpire(s, sess, args, false)
}

func doExpire(s *Server, sess *session, args [][]byte, seconds bool) resp.Value {
	n, err := strconv.ParseInt(string(args[1]), 10, 64)
	if err != nil || n <= 0 {
		return resp.SimpleError("ERR value is not an integer or out of range")
	}
	ms := n
	if seconds {
		ms = n * 1000
	}
	if s.store.SetTTL(sess.db, string(args[0]), s.store.ClockMs()+ms) {
		return resp.Integer(1)
	}
	return resp.Integer(0)
}

func ttl(s *Server, sess *session, args [][]byte) resp.Value {
	return doTTL(s, sess, args[0], true)
}

func pttl(s *Server, sess *session, args [][]byte) resp.Value {
	return doTTL(s, sess, args[0], false)
}

func doTTL(s *Server, sess *session, key []byte, seconds bool) resp.Value {
	ms, hasKey, hasTTL := s.store.TTL(sess.db, string(key), s.store.ClockMs())
	if !hasKey {
		return resp.Integer(-2)
	}
	if !hasTTL {
		return resp.Integer(-1)
	}
	if seconds {
		// Redis rounds up: (ms+999)/1000.
		return resp.Integer((ms + 999) / 1000)
	}
	return resp.Integer(ms)
}

func persist(s *Server, sess *session, args [][]byte) resp.Value {
	if s.store.Persist(sess.db, string(args[0])) {
		return resp.Integer(1)
	}
	return resp.Integer(0)
}

// commandCmd answers the introspection form clients send on connect
// ("COMMAND") with an empty array — enough to keep pipelines happy.
func commandCmd(s *Server, sess *session, args [][]byte) resp.Value {
	return resp.ArrayValue()
}
