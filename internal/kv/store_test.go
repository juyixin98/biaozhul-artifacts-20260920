package kv

import (
	"strings"
	"testing"

	"respd/internal/resp"
)

func run(s *Session, args ...string) *resp.Value {
	return s.Dispatch(args)
}

func expectErr(t *testing.T, v *resp.Value, kind string) {
	t.Helper()
	if v.Type != resp.Error {
		t.Fatalf("want error, got %+v", v)
	}
	if kind != "" && !strings.HasPrefix(v.Text, kind) {
		t.Fatalf("want error prefix %q, got %q", kind, v.Text)
	}
}

func TestBasicStringCommands(t *testing.T) {
	s := NewSession(NewStore())

	// Missing key: NULL bulk ($-1), distinct from empty string.
	if v := run(s, "GET", "missing"); v.Type != resp.NullBulk {
		t.Fatalf("missing GET: want NullBulk, got %+v", v)
	}

	if v := run(s, "SET", "k", ""); v.Type != resp.SimpleString || v.Text != "OK" {
		t.Fatalf("SET empty: %+v", v)
	}
	v := run(s, "GET", "k")
	if v.Type != resp.BulkString || len(v.Str) != 0 {
		t.Fatalf("GET empty-string key: want BulkString len 0, got %+v", v)
	}

	if v := run(s, "SET", "bin", "a\r\n\x00\xff"); !replyOK(v) {
		t.Fatalf("SET bin: %+v", v)
	}
	v = run(s, "GET", "bin")
	if v.Type != resp.BulkString || string(v.Str) != "a\r\n\x00\xff" {
		t.Fatalf("GET bin round trip: %q", v.Str)
	}

	if v := run(s, "INCR", "n"); v.Type != resp.Integer || v.Int != 1 {
		t.Fatalf("INCR new: %+v", v)
	}
	if v := run(s, "INCR", "n"); v.Int != 2 {
		t.Fatalf("INCR again: %+v", v)
	}
	expectErr(t, run(s, "INCR", "bin"), "ERR")

	if v := run(s, "STRLEN", "bin"); v.Int != 5 {
		t.Fatalf("STRLEN: %+v", v)
	}
	if v := run(s, "APPEND", "k", "xy"); v.Int != 2 {
		t.Fatalf("APPEND: %+v", v)
	}
	if v := run(s, "DEL", "n", "k", "missing"); v.Int != 2 {
		t.Fatalf("DEL count: %+v", v)
	}
	if v := run(s, "TYPE", "bin"); v.Text != "string" {
		t.Fatalf("TYPE: %+v", v)
	}
	if v := run(s, "TYPE", "gone"); v.Text != "none" {
		t.Fatalf("TYPE none: %+v", v)
	}
}

func replyOK(v *resp.Value) bool {
	return v.Type == resp.SimpleString && v.Text == "OK"
}

func TestSetNxXx(t *testing.T) {
	s := NewSession(NewStore())
	if v := run(s, "SET", "k", "v1", "NX"); !replyOK(v) {
		t.Fatalf("SET NX new: %+v", v)
	}
	if v := run(s, "SET", "k", "v2", "NX"); v.Type != resp.NullBulk {
		t.Fatalf("SET NX existing must be null: %+v", v)
	}
	if v := run(s, "SET", "other", "v", "XX"); v.Type != resp.NullBulk {
		t.Fatalf("SET XX missing must be null: %+v", v)
	}
	if v := run(s, "SET", "k", "v3", "XX"); !replyOK(v) {
		t.Fatalf("SET XX existing: %+v", v)
	}
	if v := run(s, "GET", "k"); string(v.Str) != "v3" {
		t.Fatalf("value after NX/XX: %q", v.Str)
	}
	expectErr(t, run(s, "SET", "k", "v", "BOGUS"), "ERR")
}

func TestKeysGlob(t *testing.T) {
	s := NewSession(NewStore())
	for _, k := range []string{"user:1", "user:2", "post:9", "a1", "a2"} {
		run(s, "SET", k, "x")
	}
	v := run(s, "KEYS", "user:*")
	if v.Type != resp.Array || len(v.Array) != 2 {
		t.Fatalf("KEYS user:*: %+v", v)
	}
	if string(v.Array[0].Str) != "user:1" || string(v.Array[1].Str) != "user:2" {
		t.Fatalf("KEYS sorted order: %q %q", v.Array[0].Str, v.Array[1].Str)
	}
	if v := run(s, "KEYS", "a?"); len(v.Array) != 2 {
		t.Fatalf("KEYS a?: %+v", v)
	}
	if v := run(s, "KEYS", "[uz]*"); len(v.Array) != 2 {
		t.Fatalf("KEYS class: %+v", v)
	}
}

// ---------------------------------------------------------------------------
// Transactions
// ---------------------------------------------------------------------------

func TestTransactionHappyPath(t *testing.T) {
	s := NewSession(NewStore())
	if v := run(s, "MULTI"); !replyOK(v) {
		t.Fatalf("MULTI: %+v", v)
	}
	for _, args := range [][]string{
		{"SET", "k", "1"},
		{"INCR", "k"},
		{"GET", "k"},
		{"PING"},
	} {
		if v := run(s, args...); v.Type != resp.SimpleString || v.Text != "QUEUED" {
			t.Fatalf("queue %v: %+v", args, v)
		}
	}
	v := run(s, "EXEC")
	if v.Type != resp.Array || len(v.Array) != 4 {
		t.Fatalf("EXEC: %+v", v)
	}
	if !replyOK(v.Array[0]) {
		t.Fatalf("queued SET result: %+v", v.Array[0])
	}
	if v.Array[1].Int != 2 || string(v.Array[2].Str) != "2" || v.Array[3].Text != "PONG" {
		t.Fatalf("queued results: %+v", v.Array)
	}
	// After EXEC the session is back in normal mode.
	if v := run(s, "GET", "k"); string(v.Str) != "2" {
		t.Fatalf("state after EXEC: %+v", v)
	}
}

func TestTransactionSyntaxErrorAborts(t *testing.T) {
	s := NewSession(NewStore())
	run(s, "MULTI")
	if v := run(s, "SET", "k", "1"); v.Text != "QUEUED" {
		t.Fatalf("queue SET: %+v", v)
	}

	// Queue-time error 1: unknown command.
	expectErr(t, run(s, "BOGUS", "x"), "ERR")
	// Queue-time error 2: wrong arity.
	expectErr(t, run(s, "SET", "onlykey"), "ERR")

	// Earlier valid command is still reported QUEUED; abort is deferred.
	if v := run(s, "GET", "k"); v.Text != "QUEUED" {
		t.Fatalf("queue after errors: %+v", v)
	}

	v := run(s, "EXEC")
	expectErr(t, v, "EXECABORT")

	// Nothing ran: key must not exist.
	if v := run(s, "GET", "k"); v.Type != resp.NullBulk {
		t.Fatalf("aborted tx must not mutate store: %+v", v)
	}
	// Transaction state is cleared; normal commands work again.
	if v := run(s, "SET", "k", "9"); !replyOK(v) {
		t.Fatalf("post-abort SET: %+v", v)
	}
}

func TestExecWithoutMulti(t *testing.T) {
	s := NewSession(NewStore())
	if v := run(s, "EXEC"); v.Type != resp.NullArray {
		t.Fatalf("EXEC w/o MULTI: want NullArray, got %+v", v)
	}
	expectErr(t, run(s, "DISCARD"), "ERR")
}

func TestNestedMultiAndDiscard(t *testing.T) {
	s := NewSession(NewStore())
	run(s, "MULTI")
	expectErr(t, run(s, "MULTI"), "ERR")
	run(s, "SET", "k", "v")
	if v := run(s, "DISCARD"); !replyOK(v) {
		t.Fatalf("DISCARD: %+v", v)
	}
	if v := run(s, "GET", "k"); v.Type != resp.NullBulk {
		t.Fatalf("DISCARD must drop queue: %+v", v)
	}
	expectErr(t, run(s, "DISCARD"), "ERR")
}

// Runtime errors inside EXEC are per-command errors in the reply array; they
// do NOT abort the surrounding commands.
func TestRuntimeErrorInsideExec(t *testing.T) {
	s := NewSession(NewStore())
	run(s, "SET", "bin", "notanumber")
	run(s, "MULTI")
	run(s, "INCR", "bin") // runtime error only at execution
	run(s, "SET", "after", "1")
	v := run(s, "EXEC")
	if v.Type != resp.Array || len(v.Array) != 2 {
		t.Fatalf("EXEC: %+v", v)
	}
	expectErr(t, v.Array[0], "ERR")
	if !replyOK(v.Array[1]) {
		t.Fatalf("command after runtime error must still run: %+v", v.Array[1])
	}
}

func TestArity(t *testing.T) {
	s := NewSession(NewStore())
	expectErr(t, run(s, "GET"), "ERR")
	expectErr(t, run(s, "GET", "a", "b"), "ERR")
	expectErr(t, run(s, "ECHO"), "ERR")
	expectErr(t, run(s, "SET", "k"), "ERR")
	if v := run(s, "DEL", "a", "b", "c"); v.Int != 0 { // DEL accepts many
		t.Fatalf("DEL arity: %+v", v)
	}
}
