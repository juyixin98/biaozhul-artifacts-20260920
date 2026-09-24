// Package task contains the whitelist of executable functions. Only functions
// registered here can be referenced by a DAG node: the server never executes
// arbitrary user-supplied code. All built-in functions are pure (deterministic
// total functions over JSON-shaped inputs).
package task

import (
	"context"
	"errors"
	"fmt"
	"math"
	"sort"
	"strings"
)

// Func executes one node. params is the node's Params map after $ref
// substitution; deps is the resolved result of every dependency keyed by node
// ID. Returned errors fail the attempt and trigger the retry policy. ctx is
// cancelled when the DAG is cancelled or the process is shutting down;
// long-running functions should return promptly when ctx.Done() fires. The
// built-in functions are pure and ignore ctx.
type Func func(ctx context.Context, params map[string]any, deps map[string]any) (any, error)

// Registry is an immutable whitelist of named functions.
type Registry struct {
	funcs map[string]Func
}

// NewRegistry builds a registry. nil entries are rejected.
func NewRegistry(funcs map[string]Func) *Registry {
	cp := make(map[string]Func, len(funcs))
	for name, fn := range funcs {
		if fn == nil {
			panic("task: nil function registered as " + name)
		}
		cp[name] = fn
	}
	return &Registry{funcs: cp}
}

// Get returns the named function.
func (r *Registry) Get(name string) (Func, bool) {
	fn, ok := r.funcs[name]
	return fn, ok
}

// Names lists registered function names in sorted order.
func (r *Registry) Names() []string {
	names := make([]string, 0, len(r.funcs))
	for n := range r.funcs {
		names = append(names, n)
	}
	sort.Strings(names)
	return names
}

// ResolveParams replaces {"$ref": "<nodeID>"} values with the referenced
// dependency result. References may be nested inside maps/slices. An unknown
// reference (one that is not a succeeded dependency) is an error.
func ResolveParams(params map[string]any, depResults map[string]any) (map[string]any, error) {
	if params == nil {
		return map[string]any{}, nil
	}
	out := make(map[string]any, len(params))
	for k, v := range params {
		rv, err := resolveValue(v, depResults)
		if err != nil {
			return nil, fmt.Errorf("param %q: %w", k, err)
		}
		out[k] = rv
	}
	return out, nil
}

func resolveValue(v any, depResults map[string]any) (any, error) {
	switch t := v.(type) {
	case map[string]any:
		if ref, ok := refName(t); ok {
			res, exists := depResults[ref]
			if !exists {
				return nil, fmt.Errorf("reference %q is not a succeeded dependency", ref)
			}
			return res, nil
		}
		m := make(map[string]any, len(t))
		for k, sub := range t {
			rv, err := resolveValue(sub, depResults)
			if err != nil {
				return nil, err
			}
			m[k] = rv
		}
		return m, nil
	case []any:
		s := make([]any, len(t))
		for i, sub := range t {
			rv, err := resolveValue(sub, depResults)
			if err != nil {
				return nil, err
			}
			s[i] = rv
		}
		return s, nil
	default:
		return v, nil
	}
}

// refName returns the reference target when m is exactly {"$ref": "name"}.
func refName(m map[string]any) (string, bool) {
	if len(m) != 1 {
		return "", false
	}
	raw, ok := m["$ref"]
	if !ok {
		return "", false
	}
	name, ok := raw.(string)
	return name, ok
}

// ---------------------------------------------------------------------------
// Built-in pure functions
// ---------------------------------------------------------------------------

func asFloat(v any) (float64, bool) {
	switch n := v.(type) {
	case float64:
		return n, true
	case int:
		return float64(n), true
	case int64:
		return float64(n), true
	}
	return 0, false
}

func numParam(params map[string]any, key string) (float64, error) {
	v, ok := params[key]
	if !ok {
		return 0, fmt.Errorf("missing number parameter %q", key)
	}
	n, ok := asFloat(v)
	if !ok {
		return 0, fmt.Errorf("parameter %q must be a number, got %T", key, v)
	}
	return n, nil
}

func strParam(params map[string]any, key string) (string, error) {
	v, ok := params[key]
	if !ok {
		return "", fmt.Errorf("missing string parameter %q", key)
	}
	s, ok := v.(string)
	if !ok {
		return "", fmt.Errorf("parameter %q must be a string, got %T", key, v)
	}
	return s, nil
}

// normalize returns whole floats as int64 (so JSON output stays
// integer-shaped); values outside the exact-integer range stay float64.
func normalize(n float64) any {
	if n == math.Trunc(n) && n < 1<<53 && n > -(1<<53) {
		return int64(n)
	}
	return n
}

var builtins = map[string]Func{
	// const: echo a literal value {"value": ...}.
	"const": func(_ context.Context, p map[string]any, _ map[string]any) (any, error) {
		v, ok := p["value"]
		if !ok {
			return nil, errors.New(`const requires "value"`)
		}
		return v, nil
	},

	// add: x + y (numeric).
	"add": func(_ context.Context, p map[string]any, _ map[string]any) (any, error) {
		x, err := numParam(p, "x")
		if err != nil {
			return nil, err
		}
		y, err := numParam(p, "y")
		if err != nil {
			return nil, err
		}
		return normalize(x + y), nil
	},

	// mul: x * y (numeric).
	"mul": func(_ context.Context, p map[string]any, _ map[string]any) (any, error) {
		x, err := numParam(p, "x")
		if err != nil {
			return nil, err
		}
		y, err := numParam(p, "y")
		if err != nil {
			return nil, err
		}
		return normalize(x * y), nil
	},

	// div: integer-style checked division (error on divisor 0; retries cannot
	// fix it, so it demonstrates terminal failure).
	"div": func(_ context.Context, p map[string]any, _ map[string]any) (any, error) {
		x, err := numParam(p, "x")
		if err != nil {
			return nil, err
		}
		y, err := numParam(p, "y")
		if err != nil {
			return nil, err
		}
		if y == 0 {
			return nil, errors.New("division by zero")
		}
		return normalize(x / y), nil
	},

	// sum_deps: sum every dependency result as numbers.
	"sum_deps": func(_ context.Context, _ map[string]any, deps map[string]any) (any, error) {
		if len(deps) == 0 {
			return nil, errors.New("sum_deps requires at least one dependency")
		}
		var total float64
		for id, v := range deps {
			n, ok := asFloat(v)
			if !ok {
				return nil, fmt.Errorf("dependency %q result is not a number: %v", id, v)
			}
			total += n
		}
		return normalize(total), nil
	},

	// concat: join dependency string results (or params strings) in
	// dependency declaration order is not available here, so join
	// params.values after reference resolution with sep.
	"concat": func(_ context.Context, p map[string]any, _ map[string]any) (any, error) {
		sep, _ := p["sep"].(string)
		raw, ok := p["values"]
		if !ok {
			return nil, errors.New(`concat requires "values" array`)
		}
		arr, ok := raw.([]any)
		if !ok {
			return nil, errors.New(`concat "values" must be an array`)
		}
		parts := make([]string, len(arr))
		for i, v := range arr {
			s, ok := v.(string)
			if !ok {
				return nil, fmt.Errorf("concat values[%d] must be a string, got %T", i, v)
			}
			parts[i] = s
		}
		return strings.Join(parts, sep), nil
	},

	// length: length of a string or array.
	"length": func(_ context.Context, p map[string]any, _ map[string]any) (any, error) {
		v, ok := p["value"]
		if !ok {
			return nil, errors.New(`length requires "value"`)
		}
		switch t := v.(type) {
		case string:
			return int64(len([]rune(t))), nil
		case []any:
			return int64(len(t)), nil
		default:
			return nil, fmt.Errorf("length expects string or array, got %T", v)
		}
	},

	// fail: always fails with a deterministic message — used to demonstrate
	// retries, terminal failure and skip propagation.
	"fail": func(_ context.Context, p map[string]any, _ map[string]any) (any, error) {
		msg, _ := p["message"].(string)
		if msg == "" {
			msg = "deliberate failure"
		}
		return nil, errors.New(msg)
	},
}

// Builtins returns a registry containing the documented pure functions.
func Builtins() *Registry {
	return NewRegistry(builtins)
}
