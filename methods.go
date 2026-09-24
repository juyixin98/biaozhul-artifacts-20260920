package main

import (
	"bytes"
	"context"
	"encoding/json"
	"strconv"
	"time"
)

// registerBuiltins installs the demo methods:
//
//	echo(params)      -> echoes "params" back unchanged
//	ping()            -> "pong"
//	sum(nums...)      -> total of a numeric array (ints stay exact)
//	subtract(a, b)    -> a-b, accepts positional [a,b] or named {"a":n,"b":n}
//	delay(durationMs) -> waits then returns "done"; exists to exercise
//	                      concurrency / out-of-order completion in tests.
//
// The handlers intentionally support both positional and named params so the
// examples cover the full JSON-RPC 2.0 calling convention.
func (g *Gateway) registerBuiltins() {
	g.Register("echo", methodEcho)
	g.Register("ping", methodPing)
	g.Register("sum", methodSum)
	g.Register("subtract", methodSubtract)
	g.Register("delay", methodDelay)
}

func methodEcho(_ context.Context, params json.RawMessage) (interface{}, error) {
	if len(params) == 0 {
		return nil, nil
	}
	var v interface{}
	if err := decodeUseNumber(params, &v); err != nil {
		return nil, errInvalidParams("params must be valid JSON")
	}
	return v, nil
}

func methodPing(_ context.Context, _ json.RawMessage) (interface{}, error) {
	return "pong", nil
}

// decodeParams unmarshals params into v, accepting a missing params field as
// the zero value (typical handler default) but rejecting JSON null.
func decodeParams(params json.RawMessage, v interface{}) error {
	if len(params) == 0 {
		return nil
	}
	dec := json.NewDecoder(bytes.NewReader(params))
	dec.UseNumber()
	if err := dec.Decode(v); err != nil {
		return errInvalidParams("params have the wrong shape: " + err.Error())
	}
	return nil
}

func methodSum(_ context.Context, params json.RawMessage) (interface{}, error) {
	var nums []json.Number
	if len(params) == 0 {
		return nil, errInvalidParams("sum requires an array of numbers")
	}
	// Only positional params are accepted for sum.
	if params[0] != '[' {
		return nil, errInvalidParams("sum requires an array of numbers")
	}
	if err := decodeParams(params, &nums); err != nil {
		return nil, err
	}
	intTotal := int64(0)
	allInt := true
	var floatTotal float64
	for i, n := range nums {
		if iv, err := n.Int64(); err == nil {
			intTotal += iv
			continue
		}
		fv, err := n.Float64()
		if err != nil {
			return nil, errInvalidParams("element " + strconv.Itoa(i) + " is not a finite number")
		}
		allInt = false
		floatTotal += fv
	}
	if allInt {
		return intTotal, nil
	}
	// Once any fractional value appears, return a float for the whole result.
	floatTotal += float64(intTotal)
	return floatTotal, nil
}

func methodSubtract(_ context.Context, params json.RawMessage) (interface{}, error) {
	if len(params) == 0 {
		return nil, errInvalidParams("subtract requires [a, b] or {\"a\":n,\"b\":n}")
	}
	switch params[0] {
	case '[':
		var p [2]json.Number
		if err := decodeParams(params, &p); err != nil {
			return nil, err
		}
		if p[0] == "" || p[1] == "" {
			return nil, errInvalidParams("subtract requires exactly two numbers")
		}
		return subNumbers(p[0], p[1])
	case '{':
		var p struct {
			A json.Number `json:"a"`
			B json.Number `json:"b"`
		}
		if err := decodeParams(params, &p); err != nil {
			return nil, err
		}
		if p.A == "" || p.B == "" {
			return nil, errInvalidParams("subtract requires both \"a\" and \"b\" to be numbers")
		}
		return subNumbers(p.A, p.B)
	default:
		return nil, errInvalidParams("subtract requires [a, b] or {\"a\":n,\"b\":n}")
	}
}

// subNumbers keeps integer results exact and falls back to float64 otherwise.
func subNumbers(a, b json.Number) (interface{}, error) {
	ai, aerr := a.Int64()
	bi, berr := b.Int64()
	if aerr == nil && berr == nil {
		return ai - bi, nil
	}
	af, err := a.Float64()
	if err != nil {
		return nil, errInvalidParams("\"a\" is not a finite number")
	}
	bf, err := b.Float64()
	if err != nil {
		return nil, errInvalidParams("\"b\" is not a finite number")
	}
	return af - bf, nil
}

func methodDelay(ctx context.Context, params json.RawMessage) (interface{}, error) {
	var p []json.Number
	if len(params) != 0 {
		if err := decodeParams(params, &p); err != nil {
			return nil, err
		}
		if len(p) != 1 {
			return nil, errInvalidParams("delay requires one number of milliseconds")
		}
	}
	ms := int64(50)
	if len(p) == 1 {
		v, err := p[0].Int64()
		if err != nil {
			return nil, errInvalidParams("duration must be an integer number of milliseconds")
		}
		if v < 0 {
			return nil, errInvalidParams("duration must be non-negative")
		}
		if v > 30000 {
			v = 30000
		}
		ms = v
	}
	select {
	case <-time.After(time.Duration(ms) * time.Millisecond):
		return "done", nil
	case <-ctx.Done():
		return nil, ctx.Err()
	}
}
