package dag

import (
	"context"
	"fmt"
	"sort"
	"strconv"
	"strings"
	"time"
)

// Input is passed to every task invocation. All tasks are pure functions
// of (Params, Upstream, Attempt): identical inputs must yield identical
// outputs. The only exception allowed is explicit cancellation/context use
// for long-running demo helpers (sleep), which produce no observable value.
type Input struct {
	// Params are the static parameters declared on the node spec.
	Params map[string]interface{}
	// Upstream maps each dependency node id to its cached success result.
	Upstream map[string]interface{}
	// Attempt is 1-based within the current activation.
	Attempt int
	// TotalAttempt is 1-based over the whole lifetime of the node (it
	// survives reactivation after retry/restart). It lets "flaky" fail
	// deterministically for a chosen number of lifetime attempts.
	TotalAttempt int
}

// TaskFunc is the signature every whitelisted task must implement.
type TaskFunc func(ctx context.Context, in Input) (interface{}, error)

// DefaultRegistry returns the built-in whitelist:
//
//	noop()                         -> nil
//	identity(params.value)         -> params.value
//	add() / mul()                  -> sum / product over params.values
//	                                                and upstream results
//	concat(sep)                    -> join of all input values as strings
//	collect()                      -> {"params": ..., "upstream": {id: v}}
//	fail(message)                  -> always errors
//	flaky(fail_times, succeed_with)-> errors for the first fail_times
//	                                 *lifetime* attempts, then succeeds;
//	                                 state lives entirely in TotalAttempt,
//	                                 so it survives restarts
//	sleep(ms)                      -> waits up to ms milliseconds (demo aid
//	                                 for cancellation tests); returns nil
func DefaultRegistry() map[string]TaskFunc {
	return map[string]TaskFunc{
		"noop":     taskNoop,
		"identity": taskIdentity,
		"add":      taskAdd,
		"mul":      taskMul,
		"concat":   taskConcat,
		"collect":  taskCollect,
		"fail":     taskFail,
		"flaky":    taskFlaky,
		"sleep":    taskSleep,
	}
}

func taskNoop(context.Context, Input) (interface{}, error) { return nil, nil }

func taskIdentity(_ context.Context, in Input) (interface{}, error) {
	if v, ok := in.Params["value"]; ok {
		return v, nil
	}
	return nil, nil
}

// numericInputs collects params.values (a JSON array) plus every upstream
// result in dependency-id order.
func numericInputs(in Input) ([]float64, error) {
	var out []float64
	for _, v := range sliceParam(in.Params["values"]) {
		f, ok := toFloat(v)
		if !ok {
			return nil, fmt.Errorf("non-numeric value %v", v)
		}
		out = append(out, f)
	}
	for _, id := range sortedKeys(in.Upstream) {
		f, ok := toFloat(in.Upstream[id])
		if !ok {
			return nil, fmt.Errorf("upstream %q produced non-numeric value %v", id, in.Upstream[id])
		}
		out = append(out, f)
	}
	return out, nil
}

func taskAdd(_ context.Context, in Input) (interface{}, error) {
	vs, err := numericInputs(in)
	if err != nil {
		return nil, err
	}
	var sum float64
	for _, v := range vs {
		sum += v
	}
	return normalizeNumber(sum), nil
}

func taskMul(_ context.Context, in Input) (interface{}, error) {
	vs, err := numericInputs(in)
	if err != nil {
		return nil, err
	}
	prod := 1.0
	for _, v := range vs {
		prod *= v
	}
	return normalizeNumber(prod), nil
}

func taskConcat(_ context.Context, in Input) (interface{}, error) {
	sep := stringParam(in.Params, "sep", "")
	var parts []string
	for _, v := range sliceParam(in.Params["values"]) {
		parts = append(parts, stringify(v))
	}
	for _, id := range sortedKeys(in.Upstream) {
		parts = append(parts, stringify(in.Upstream[id]))
	}
	return strings.Join(parts, sep), nil
}

func taskCollect(_ context.Context, in Input) (interface{}, error) {
	return map[string]interface{}{
		"params":   in.Params,
		"upstream": in.Upstream,
	}, nil
}

func taskFail(_ context.Context, in Input) (interface{}, error) {
	msg := stringParam(in.Params, "message", "task configured to always fail")
	return nil, fmt.Errorf("%s (attempt %d)", msg, in.TotalAttempt)
}

func taskFlaky(_ context.Context, in Input) (interface{}, error) {
	failTimes := int(intParam(in.Params, "fail_times", 1))
	if in.TotalAttempt <= failTimes {
		return nil, fmt.Errorf("flaky failure %d of %d configured", in.TotalAttempt, failTimes)
	}
	if v, ok := in.Params["succeed_with"]; ok {
		return v, nil
	}
	return "ok", nil
}

func taskSleep(ctx context.Context, in Input) (interface{}, error) {
	ms := intParam(in.Params, "ms", 100)
	if ms < 0 {
		ms = 0
	}
	t := time.NewTimer(time.Duration(ms) * time.Millisecond)
	defer t.Stop()
	select {
	case <-ctx.Done():
		return nil, ctx.Err()
	case <-t.C:
		return nil, nil
	}
}

// ---------------------------------------------------------------- helpers

func sliceParam(v interface{}) []interface{} {
	if v == nil {
		return nil
	}
	if s, ok := v.([]interface{}); ok {
		return s
	}
	return nil
}

func stringParam(p map[string]interface{}, key, def string) string {
	if p != nil {
		if v, ok := p[key]; ok {
			if s, ok := v.(string); ok {
				return s
			}
		}
	}
	return def
}

func intParam(p map[string]interface{}, key string, def int64) int64 {
	if p != nil {
		if v, ok := p[key]; ok {
			if f, ok := toFloat(v); ok {
				return int64(f)
			}
		}
	}
	return def
}

func toFloat(v interface{}) (float64, bool) {
	switch x := v.(type) {
	case float64:
		return x, true
	case float32:
		return float64(x), true
	case int:
		return float64(x), true
	case int64:
		return float64(x), true
	}
	return 0, false
}

func normalizeNumber(f float64) interface{} {
	if f == float64(int64(f)) && !strings.Contains(strconv.FormatFloat(f, 'g', -1, 64), "e") {
		return int64(f)
	}
	return f
}

func stringify(v interface{}) string {
	switch x := v.(type) {
	case nil:
		return ""
	case string:
		return x
	case bool:
		return strconv.FormatBool(x)
	case float64:
		if x == float64(int64(x)) {
			return strconv.FormatInt(int64(x), 10)
		}
		return strconv.FormatFloat(x, 'g', -1, 64)
	default:
		return fmt.Sprintf("%v", x)
	}
}

func sortedKeys(m map[string]interface{}) []string {
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	return keys
}
