package kv

// Spec describes one command for arity validation and the /commands API.
//
// Arity follows the Redis convention: a positive number means exactly that
// many arguments INCLUDING the command name; a negative number -N means at
// least N arguments including the command name.
type Spec struct {
	Name    string `json:"name"`
	Arity   int    `json:"arity"`
	Summary string `json:"summary"`
}

// specs is the command table. It is intentionally small: only string keys and
// transaction control are implemented.
var specs = []Spec{
	{Name: "PING", Arity: -1, Summary: "Reply PONG, or echo the argument back."},
	{Name: "ECHO", Arity: 2, Summary: "Return the argument."},
	{Name: "SET", Arity: -3, Summary: "Set key to value; options NX/XX."},
	{Name: "GET", Arity: 2, Summary: "Get the value of key; null bulk if missing."},
	{Name: "DEL", Arity: -2, Summary: "Delete one or more keys; returns count removed."},
	{Name: "INCR", Arity: 2, Summary: "Atomically increment the integer at key by 1."},
	{Name: "APPEND", Arity: 3, Summary: "Append value to key; returns new length."},
	{Name: "STRLEN", Arity: 2, Summary: "Byte length of the value at key."},
	{Name: "TYPE", Arity: 2, Summary: "Type of the key: string or none."},
	{Name: "KEYS", Arity: 2, Summary: "List keys matching a glob pattern."},
	{Name: "MULTI", Arity: 1, Summary: "Begin a transaction; subsequent commands queue."},
	{Name: "EXEC", Arity: 1, Summary: "Execute queued commands atomically."},
	{Name: "DISCARD", Arity: 1, Summary: "Abort the transaction and clear the queue."},
}

var specIndex = func() map[string]Spec {
	m := make(map[string]Spec, len(specs))
	for _, sp := range specs {
		m[sp.Name] = sp
	}
	return m
}()

// Commands returns a copy of the command table for /commands.
func Commands() []Spec {
	out := make([]Spec, len(specs))
	copy(out, specs)
	return out
}

// arityOK checks a full argument vector (args[0] is the command name).
func arityOK(args []string) bool {
	sp, ok := specIndex[args[0]]
	if !ok {
		return false
	}
	n := len(args)
	if sp.Arity > 0 {
		return n == sp.Arity
	}
	return n >= -sp.Arity
}
