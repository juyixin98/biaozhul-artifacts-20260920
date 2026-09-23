package raft

import (
	"strings"
	"sync"
)

// KVStateMachine is a trivial key/value state machine. Commands:
//
//	"SET <key> <value>"
//	"DELETE <key>"
//	"NOOP"
//
// Anything else is applied as a no-op command (and still recorded in the log).
type KVStateMachine struct {
	mu sync.Mutex
	kv map[string]string
}

func NewKVStateMachine() *KVStateMachine {
	return &KVStateMachine{kv: map[string]string{}}
}

// Reset empties the state machine; called when the node restarts so the
// committed prefix can be replayed from storage.
func (f *KVStateMachine) Reset() {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.kv = map[string]string{}
}

func (f *KVStateMachine) Apply(index, term int, command string) {
	fields := strings.Fields(command)
	if len(fields) == 0 {
		return
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	switch fields[0] {
	case "SET":
		if len(fields) >= 3 {
			f.kv[fields[1]] = strings.Join(fields[2:], " ")
		}
	case "DELETE":
		if len(fields) >= 2 {
			delete(f.kv, fields[1])
		}
	case "NOOP":
		// intentionally empty
	}
}

func (f *KVStateMachine) Get(key string) (string, bool) {
	f.mu.Lock()
	defer f.mu.Unlock()
	v, ok := f.kv[key]
	return v, ok
}

// Snapshot returns a copy of the current key/value state.
func (f *KVStateMachine) Snapshot() map[string]string {
	f.mu.Lock()
	defer f.mu.Unlock()
	out := make(map[string]string, len(f.kv))
	for k, v := range f.kv {
		out[k] = v
	}
	return out
}
