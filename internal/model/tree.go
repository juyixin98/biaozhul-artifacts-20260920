// Package model defines the behavior tree definition model, validation and
// content-addressing (canonical JSON + SHA-256).
package model

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"regexp"
)

// Node statuses propagated by ticks. Canceled is an internal bookkeeping
// status (an action or subtree that was canceled by a parent decision); it is
// failure-equivalent for parent propagation but kept distinct for auditing.
const (
	StatusRunning  = "running"
	StatusSuccess  = "success"
	StatusFailure  = "failure"
	StatusCanceled = "canceled"
)

// Node kinds.
const (
	KindRoot      = "root"
	KindSequence  = "sequence"
	KindFallback  = "fallback"
	KindParallel  = "parallel"
	KindTimeout   = "timeout"
	KindAction    = "action"
	KindInverter  = "inverter" // small extra decorator: useful for fallback examples
	KindSucceeder = "succeeder"
)

// Node is one node of a tree definition. Definitions are JSON-decoded into
// this struct and validated before publishing; published definitions never
// change, so runtime code only ever sees validated trees.
type Node struct {
	ID            string           `json:"id"`
	Kind          string           `json:"kind"`
	Name          string           `json:"name,omitempty"`
	Stub          string           `json:"stub,omitempty"`           // action only
	Args          *json.RawMessage `json:"args,omitempty"`           // action only: raw stub arguments
	NonIdempotent bool             `json:"non_idempotent,omitempty"` // action: physical side effect must never repeat
	MS            int              `json:"ms,omitempty"`             // timeout only: wall-clock budget
	Success       int              `json:"success,omitempty"`        // parallel: success threshold (0 => number of children)
	Failure       int              `json:"failure,omitempty"`        // parallel: failure threshold (0 => number of children)
	Children      []*Node          `json:"children,omitempty"`
	Child         *Node            `json:"child,omitempty"`
}

// Tree is a published (or to-be-published) tree definition.
type Tree struct {
	Name string `json:"name"`
	Root *Node  `json:"root"`
}

var idRe = regexp.MustCompile(`^[a-zA-Z0-9_.-]{1,80}$`)

var validKinds = map[string]bool{
	KindRoot: true, KindSequence: true, KindFallback: true,
	KindParallel: true, KindTimeout: true, KindAction: true,
	KindInverter: true, KindSucceeder: true,
}

// Validate checks structural and semantic validity of a tree definition.
// Every node must carry a stable id that is unique within the tree: the
// execution layer keys persisted state on these ids, so duplicate ids are
// rejected at publish time.
func (t *Tree) Validate() error {
	if t.Name == "" {
		return fmt.Errorf("tree name is required")
	}
	if t.Root == nil {
		return fmt.Errorf("tree %q: root is required", t.Name)
	}
	seen := map[string]bool{}
	var walk func(n *Node, path string, allowRoot bool) error
	walk = func(n *Node, path string, allowRoot bool) error {
		if n == nil {
			return fmt.Errorf("%s: null node", path)
		}
		if !idRe.MatchString(n.ID) {
			return fmt.Errorf("%s: invalid node id %q (^[a-zA-Z0-9_.-]{1,80}$)", path, n.ID)
		}
		if seen[n.ID] {
			return fmt.Errorf("%s: duplicate node id %q", path, n.ID)
		}
		seen[n.ID] = true
		if allowRoot && n.Kind != KindRoot {
			return fmt.Errorf("node %q: root must have kind %q", n.ID, KindRoot)
		}
		if !allowRoot && !validKinds[n.Kind] {
			return fmt.Errorf("node %q: unknown kind %q", n.ID, n.Kind)
		}
		switch n.Kind {
		case KindRoot, KindSequence, KindFallback:
			if len(n.Children) == 0 {
				return fmt.Errorf("node %q: %s requires children", n.ID, n.Kind)
			}
		case KindParallel:
			if len(n.Children) == 0 {
				return fmt.Errorf("node %q: parallel requires children", n.ID)
			}
			s, f := n.ParallelThresholds(len(n.Children))
			if s <= 0 || f <= 0 || s > len(n.Children) || f > len(n.Children) {
				return fmt.Errorf("node %q: bad parallel thresholds", n.ID)
			}
			// With succ+fail = children+1 there is at most one outcome
			// assignment (a single running child) that decides neither; the
			// node then correctly reports running. Beyond that the
			// thresholds could leave a fully decided child set undecided.
			if s+f > len(n.Children)+1 {
				return fmt.Errorf("node %q: success/failure thresholds can never decide (s=%d f=%d children=%d)",
					n.ID, s, f, len(n.Children))
			}
		case KindTimeout:
			if n.Child == nil {
				return fmt.Errorf("node %q: timeout requires a child", n.ID)
			}
			if n.MS <= 0 {
				return fmt.Errorf("node %q: timeout requires positive ms", n.ID)
			}
		case KindAction:
			if n.Stub == "" {
				return fmt.Errorf("node %q: action requires a stub", n.ID)
			}
			if len(n.Children) > 0 || n.Child != nil {
				return fmt.Errorf("node %q: action is a leaf", n.ID)
			}
		case KindInverter, KindSucceeder:
			if n.Child == nil {
				return fmt.Errorf("node %q: %s requires a child", n.ID, n.Kind)
			}
		}
		for i, c := range n.Children {
			if err := walk(c, path+"/"+n.ID+fmt.Sprintf("[%d]", i), false); err != nil {
				return err
			}
		}
		if n.Child != nil {
			if err := walk(n.Child, path+"/"+n.ID+"/child", false); err != nil {
				return err
			}
		}
		return nil
	}
	return walk(t.Root, "$", true)
}

// ParallelThresholds resolves the effective success/failure thresholds,
// applying defaults: unspecified (0) means "all children".
func (n *Node) ParallelThresholds(childCount int) (success, failure int) {
	success = n.Success
	if success == 0 {
		success = childCount
	}
	failure = n.Failure
	if failure == 0 {
		failure = childCount
	}
	return
}

// Walk visits every node in depth-first order.
func Walk(n *Node, fn func(*Node) bool) bool {
	if fn(n) {
		return true
	}
	for _, c := range n.Children {
		if Walk(c, fn) {
			return true
		}
	}
	if n.Child != nil && Walk(n.Child, fn) {
		return true
	}
	return false
}

// Find returns the node with the given id or nil.
func (t *Tree) Find(id string) *Node {
	var found *Node
	Walk(t.Root, func(n *Node) bool {
		if n.ID == id {
			found = n
			return true
		}
		return false
	})
	return found
}

// canonicalEncode produces a deterministic JSON encoding. Go's encoding/json
// already emits struct fields in declaration order (struct tags respected),
// and json.RawMessage is emitted verbatim; action args are re-canonicalized
// first so formatting differences in the input cannot change the hash.
func canonicalEncode(v any) ([]byte, error) {
	return json.Marshal(v)
}

// canonicalNode rebuilds a node with canonicalized args (decoded/re-encoded so
// key order and whitespace are normalized) and children in definition order.
func canonicalNode(n *Node) (map[string]any, error) {
	m := map[string]any{"id": n.ID, "kind": n.Kind}
	if n.Name != "" {
		m["name"] = n.Name
	}
	if n.Stub != "" {
		m["stub"] = n.Stub
	}
	if n.Args != nil {
		var argsAny any
		if err := json.Unmarshal(*n.Args, &argsAny); err != nil {
			return nil, fmt.Errorf("node %q: invalid args JSON: %w", n.ID, err)
		}
		m["args"] = argsAny
	}
	if n.MS != 0 {
		m["ms"] = n.MS
	}
	if n.Success != 0 {
		m["success"] = n.Success
	}
	if n.Failure != 0 {
		m["failure"] = n.Failure
	}
	if len(n.Children) > 0 {
		cs := make([]any, 0, len(n.Children))
		for _, c := range n.Children {
			cc, err := canonicalNode(c)
			if err != nil {
				return nil, err
			}
			cs = append(cs, cc)
		}
		m["children"] = cs
	}
	if n.Child != nil {
		cc, err := canonicalNode(n.Child)
		if err != nil {
			return nil, err
		}
		m["child"] = cc
	}
	return m, nil
}

// Hash returns the content hash of the tree definition: SHA-256 over the
// canonical JSON encoding, hex encoded. Two publishes with identical
// semantics hash identically regardless of input formatting/key order.
func (t *Tree) Hash() (string, error) {
	rootMap, err := canonicalNode(t.Root)
	if err != nil {
		return "", err
	}
	canon := map[string]any{"name": t.Name, "root": rootMap}
	// Marshal map keys sorted (Go sorts map keys by default).
	b, err := canonicalEncode(canon)
	if err != nil {
		return "", err
	}
	sum := sha256.Sum256(b)
	return hex.EncodeToString(sum[:]), nil
}
