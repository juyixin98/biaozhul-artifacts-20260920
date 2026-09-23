package runner

import (
	"fmt"

	"fencinglease/internal/resource"
	"fencinglease/internal/sim"
)

// evaluate collects commits from resource logs, derives the global safety
// invariants and then evaluates the scenario's explicit expectations.
func evaluate(sc *Scenario, eng *sim.Engine, resByName map[string]*resource.Resource) *Result {
	res := &Result{Name: sc.Name, Seed: sc.Seed, EndTime: eng.Now(), Traces: eng.Records()}

	// Commits from each fenced resource, in acceptance order.
	for _, rc := range sc.Resources {
		r := resByName[rc.Name]
		for _, e := range r.Log() {
			res.Commits = append(res.Commits, Commit{
				Time: e.Time, Resource: rc.Name, Node: e.Node, Fence: e.Fence, Value: e.Value,
			})
		}
	}

	// Built-in invariants, checked for every fenced resource.
	for _, rc := range sc.Resources {
		r := resByName[rc.Name]
		var prev int64
		violated := false
		for i, e := range r.Log() {
			if i > 0 && e.Fence <= prev {
				res.Checks = append(res.Checks, Check{
					Kind: "fence_monotonic",
					Pass: false,
					Detail: fmt.Sprintf("resource %s: fence %d after %d at t=%d (%s)",
						rc.Name, e.Fence, prev, e.Time, e.Node),
				})
				violated = true
			}
			prev = e.Fence
		}
		if rc.Fenced && len(r.Log()) > 0 && !violated {
			res.Checks = append(res.Checks, Check{
				Kind:   "fence_monotonic",
				Pass:   true,
				Detail: fmt.Sprintf("resource %s: %d commits, fences strictly increasing", rc.Name, len(r.Log())),
			})
		}
	}

	// Explicit expectations.
	for _, ex := range sc.Expect {
		res.Checks = append(res.Checks, evalExpect(ex, eng.Records(), res))
	}

	res.OK = true
	for _, c := range res.Checks {
		if !c.Pass {
			res.OK = false
			break
		}
	}
	return res
}

func findRecords(records []sim.Record, node, recType string) []sim.Record {
	var out []sim.Record
	for _, r := range records {
		if node != "" && r.Node != node {
			continue
		}
		if recType != "" && r.Type != recType {
			continue
		}
		out = append(out, r)
	}
	return out
}

func evalAcceptOrder(ex Expect, res *Result) Check {
	var got []string
	for _, c := range res.Commits {
		if ex.Resource != "" && c.Resource != ex.Resource {
			continue
		}
		got = append(got, c.Node)
	}
	pass := len(got) == len(ex.Order)
	if pass {
		for i := range got {
			if got[i] != ex.Order[i] {
				pass = false
				break
			}
		}
	}
	detail := fmt.Sprintf("resource=%s accepted node order=%v expected=%v", ex.Resource, got, ex.Order)
	return Check{Kind: ex.Kind, Pass: pass, Detail: detail}
}

func evalExpect(ex Expect, records []sim.Record, res *Result) Check {
	switch ex.Kind {
	case "record_count":
		rs := findRecords(records, ex.Node, ex.Type)
		n := len(rs)
		pass := true
		detail := fmt.Sprintf("count(%s/%s)=%d", ex.Node, ex.Type, n)
		if ex.Min != nil && n < *ex.Min {
			pass, detail = false, detail+fmt.Sprintf(" < min %d", *ex.Min)
		}
		if ex.Max != nil && n > *ex.Max {
			pass, detail = false, detail+fmt.Sprintf(" > max %d", *ex.Max)
		}
		return Check{Kind: ex.Kind, Pass: pass, Detail: detail}

	case "records_exist":
		rs := findRecords(records, ex.Node, ex.Type)
		return Check{Kind: ex.Kind, Pass: len(rs) > 0,
			Detail: fmt.Sprintf("%s/%s found=%d", ex.Node, ex.Type, len(rs))}

	case "record_absent":
		rs := findRecords(records, ex.Node, ex.Type)
		return Check{Kind: ex.Kind, Pass: len(rs) == 0,
			Detail: fmt.Sprintf("%s/%s found=%d", ex.Node, ex.Type, len(rs))}

	case "accepted":
		n := 0
		var fences []int64
		for _, c := range res.Commits {
			if ex.Resource != "" && c.Resource != ex.Resource {
				continue
			}
			if ex.Node != "" && c.Node != ex.Node {
				continue
			}
			n++
			fences = append(fences, c.Fence)
		}
		pass := true
		if ex.Min != nil && n < *ex.Min {
			pass = false
		}
		if ex.Max != nil && n > *ex.Max {
			pass = false
		}
		return Check{Kind: ex.Kind, Pass: pass,
			Detail: fmt.Sprintf("accepted by resource=%s node=%s: %d (fences %v)", ex.Resource, ex.Node, n, fences)}

	case "rejected":
		// resource.rejected is emitted by the resource node; Fields.node is
		// the submitting client, Fields.reason the rejection cause.
		rs := findRecords(records, "", "resource.rejected")
		n := 0
		var reasons []string
		for _, r := range rs {
			if ex.Resource != "" && fieldString(r.Fields, "resource") != ex.Resource {
				continue
			}
			if ex.Node != "" && fieldString(r.Fields, "node") != ex.Node {
				continue
			}
			if ex.Field != "" && fieldString(r.Fields, "reason") != ex.Field {
				continue
			}
			n++
			reasons = append(reasons, fieldString(r.Fields, "reason"))
		}
		pass := true
		if ex.Min != nil && n < *ex.Min {
			pass = false
		}
		if ex.Max != nil && n > *ex.Max {
			pass = false
		}
		return Check{Kind: ex.Kind, Pass: pass,
			Detail: fmt.Sprintf("rejections resource=%s node=%s reason=%q: %d %v", ex.Resource, ex.Node, ex.Field, n, reasons)}

	case "last_water":
		var hw int64
		for _, c := range res.Commits {
			if c.Resource == ex.Resource && c.Fence > hw {
				hw = c.Fence
			}
		}
		return Check{Kind: ex.Kind, Pass: float64(hw) == ex.Equals,
			Detail: fmt.Sprintf("resource %s high-water=%d expected=%v", ex.Resource, hw, ex.Equals)}

	case "granted_fences":
		rs := findRecords(records, nodeSvc, "lock.granted")
		var got []int64
		for _, r := range rs {
			if ex.Resource != "" && fieldString(r.Fields, "resource") != ex.Resource {
				continue
			}
			got = append(got, fieldInt64(r.Fields, "fence"))
		}
		return Check{Kind: ex.Kind, Pass: true, Detail: fmt.Sprintf("granted fences=%v", got)}

	case "accepted_order":
		return evalAcceptOrder(ex, res)

	default:
		return Check{Kind: ex.Kind, Pass: false, Detail: "unknown expectation kind: " + ex.Kind}
	}
}

const nodeSvc = "lockserver"

func fieldString(m map[string]any, k string) string {
	if v, ok := m[k]; ok {
		if s, ok := v.(string); ok {
			return s
		}
	}
	return ""
}

func fieldInt64(m map[string]any, k string) int64 {
	switch v := m[k].(type) {
	case int64:
		return v
	case int:
		return int64(v)
	case float64:
		return int64(v)
	}
	return 0
}
