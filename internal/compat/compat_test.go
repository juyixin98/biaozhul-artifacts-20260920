package compat

import (
	"testing"

	"contractcheck/internal/schema"
)

func mustParse(t *testing.T, doc string) *schema.Schema {
	t.Helper()
	s, err := schema.Parse([]byte(doc))
	if err != nil {
		t.Fatalf("Parse(%s): %v", doc, err)
	}
	return s
}

func TestCheck(t *testing.T) {
	cases := []struct {
		name       string
		direction  Direction
		old, new   string
		wantStatus string
		wantPath   string // empty means no finding expected
	}{
		{
			name:       "request: new required field is incompatible",
			direction:  Request,
			old:        `{"type":"object","required":["a"],"properties":{"a":{"type":"string"},"b":{"type":"string"}}}`,
			new:        `{"type":"object","required":["a","b"],"properties":{"a":{"type":"string"},"b":{"type":"string"}}}`,
			wantStatus: StatusIncompatible,
			wantPath:   "$",
		},
		{
			name:       "response: new required field is a compatible tightening",
			direction:  Response,
			old:        `{"type":"object","required":["a"],"properties":{"a":{"type":"string"},"b":{"type":"string"}}}`,
			new:        `{"type":"object","required":["a","b"],"properties":{"a":{"type":"string"},"b":{"type":"string"}}}`,
			wantStatus: StatusCompatible,
		},
		{
			name:       "request: removed required field is a compatible relaxation",
			direction:  Request,
			old:        `{"type":"object","required":["a","b"],"properties":{"a":{"type":"string"},"b":{"type":"string"}}}`,
			new:        `{"type":"object","required":["a"],"properties":{"a":{"type":"string"},"b":{"type":"string"}}}`,
			wantStatus: StatusCompatible,
		},
		{
			name:       "response: removed required field is incompatible",
			direction:  Response,
			old:        `{"type":"object","required":["a","b"],"properties":{"a":{"type":"string"},"b":{"type":"string"}}}`,
			new:        `{"type":"object","required":["a"],"properties":{"a":{"type":"string"},"b":{"type":"string"}}}`,
			wantStatus: StatusIncompatible,
			wantPath:   "$",
		},
		{
			name:       "request: narrowed enum is incompatible",
			direction:  Request,
			old:        `{"enum":["USD","EUR","CNY"]}`,
			new:        `{"enum":["USD","EUR"]}`,
			wantStatus: StatusIncompatible,
			wantPath:   "$",
		},
		{
			name:       "response: narrowed enum is a compatible tightening",
			direction:  Response,
			old:        `{"enum":["USD","EUR","CNY"]}`,
			new:        `{"enum":["USD","EUR"]}`,
			wantStatus: StatusCompatible,
		},
		{
			name:       "response: widened enum is incompatible",
			direction:  Response,
			old:        `{"enum":["ok","failed"]}`,
			new:        `{"enum":["ok","failed","pending"]}`,
			wantStatus: StatusIncompatible,
			wantPath:   "$",
		},
		{
			name:       "request: raised minimum is incompatible",
			direction:  Request,
			old:        `{"type":"number","minimum":0}`,
			new:        `{"type":"number","minimum":1}`,
			wantStatus: StatusIncompatible,
			wantPath:   "$",
		},
		{
			name:       "request: lowered minimum is a compatible relaxation",
			direction:  Request,
			old:        `{"type":"number","minimum":1}`,
			new:        `{"type":"number","minimum":0}`,
			wantStatus: StatusCompatible,
		},
		{
			name:       "response: widened maximum is incompatible",
			direction:  Response,
			old:        `{"type":"integer","maximum":30}`,
			new:        `{"type":"integer","maximum":60}`,
			wantStatus: StatusIncompatible,
			wantPath:   "$",
		},
		{
			name:       "request: exclusive bound replacing inclusive is incompatible",
			direction:  Request,
			old:        `{"type":"number","minimum":0}`,
			new:        `{"type":"number","exclusiveMinimum":0}`,
			wantStatus: StatusIncompatible,
			wantPath:   "$",
		},
		{
			name:       "request: narrowed type is incompatible",
			direction:  Request,
			old:        `{"type":["string","integer"]}`,
			new:        `{"type":"string"}`,
			wantStatus: StatusIncompatible,
			wantPath:   "$",
		},
		{
			name:       "request: widened type is a compatible relaxation",
			direction:  Request,
			old:        `{"type":"string"}`,
			new:        `{"type":["string","integer"]}`,
			wantStatus: StatusCompatible,
		},
		{
			name:       "nested property violation reports the nested path",
			direction:  Request,
			old:        `{"type":"object","properties":{"user":{"type":"object","properties":{"age":{"type":"integer","minimum":0}}}}}`,
			new:        `{"type":"object","properties":{"user":{"type":"object","properties":{"age":{"type":"integer","minimum":18}}}}}`,
			wantStatus: StatusIncompatible,
			wantPath:   "$.user.age",
		},
		{
			name:       "array items are checked recursively",
			direction:  Response,
			old:        `{"type":"array","items":{"enum":["a","b","c"]}}`,
			new:        `{"type":"array","items":{"enum":["a","b","c","d"]}}`,
			wantStatus: StatusIncompatible,
			wantPath:   "$[*]",
		},
		{
			name:       "unsupported keyword yields unknown, not a pass",
			direction:  Request,
			old:        `{"type":"string"}`,
			new:        `{"type":"string","pattern":"^[a-z]+$"}`,
			wantStatus: StatusUnknown,
		},
		{
			name:       "unsupported keyword in nested node yields unknown",
			direction:  Response,
			old:        `{"type":"object","properties":{"id":{"type":"string"}}}`,
			new:        `{"type":"object","properties":{"id":{"type":"string","format":"uuid"}}}`,
			wantStatus: StatusUnknown,
		},
		{
			name:       "incompatible beats unknown",
			direction:  Request,
			old:        `{"type":"object","required":["a"],"properties":{"a":{"type":"string"}}}`,
			new:        `{"type":"object","required":["a","b"],"properties":{"a":{"type":"string"},"b":{"type":"string","pattern":"^x$"}}}`,
			wantStatus: StatusIncompatible,
			wantPath:   "$",
		},
		{
			name:       "request: any type narrowed to string is incompatible",
			direction:  Request,
			old:        `{}`,
			new:        `{"type":"string"}`,
			wantStatus: StatusIncompatible,
			wantPath:   "$",
		},
		{
			name:       "request: enum added where none existed is incompatible",
			direction:  Request,
			old:        `{"type":"integer"}`,
			new:        `{"type":"integer","enum":[7]}`,
			wantStatus: StatusIncompatible,
			wantPath:   "$",
		},
		{
			name:       "request: upper bound added where none existed is incompatible",
			direction:  Request,
			old:        `{"type":"number"}`,
			new:        `{"type":"number","maximum":10}`,
			wantStatus: StatusIncompatible,
			wantPath:   "$",
		},
		{
			name:       "response: lower bound added where none existed is incompatible",
			direction:  Response,
			old:        `{"type":"number","minimum":0}`,
			new:        `{"type":"number"}`,
			wantStatus: StatusIncompatible,
			wantPath:   "$",
		},
		{
			name:       "response: exclusive upper bound replacing inclusive is incompatible",
			direction:  Response,
			old:        `{"type":"number","exclusiveMaximum":5}`,
			new:        `{"type":"number","maximum":5}`,
			wantStatus: StatusIncompatible,
			wantPath:   "$",
		},
		{
			name:       "identical schemas are compatible in both directions",
			direction:  Response,
			old:        `{"type":"object","required":["a"],"properties":{"a":{"type":"integer","minimum":0,"maximum":9}}}`,
			new:        `{"type":"object","required":["a"],"properties":{"a":{"type":"integer","minimum":0,"maximum":9}}}`,
			wantStatus: StatusCompatible,
		},
		{
			name:       "property only on the new side is not an incompatibility by itself",
			direction:  Request,
			old:        `{"type":"object","properties":{"a":{"type":"string"}}}`,
			new:        `{"type":"object","properties":{"a":{"type":"string"},"b":{"type":"integer"}}}`,
			wantStatus: StatusCompatible,
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			res := Check(mustParse(t, tc.old), mustParse(t, tc.new), tc.direction)
			if res.Status != tc.wantStatus {
				t.Fatalf("status = %s, want %s (findings=%+v unknowns=%+v)",
					res.Status, tc.wantStatus, res.Incompatibilities, res.Unknowns)
			}
			if tc.wantPath == "" {
				return
			}
			if len(res.Incompatibilities) == 0 {
				t.Fatal("expected at least one incompatibility")
			}
			if res.Incompatibilities[0].Path != tc.wantPath {
				t.Fatalf("path = %s, want %s", res.Incompatibilities[0].Path, tc.wantPath)
			}
		})
	}
}

func TestCounterexamples(t *testing.T) {
	t.Run("new required field: example object omits it", func(t *testing.T) {
		res := Check(
			mustParse(t, `{"type":"object","required":["a"],"properties":{"a":{"type":"string"},"b":{"type":"integer"}}}`),
			mustParse(t, `{"type":"object","required":["a","b"],"properties":{"a":{"type":"string"},"b":{"type":"integer"}}}`),
			Request,
		)
		if len(res.Incompatibilities) != 1 {
			t.Fatalf("findings = %+v", res.Incompatibilities)
		}
		cex, ok := res.Incompatibilities[0].Counterexample.(map[string]any)
		if !ok {
			t.Fatalf("counterexample = %#v", res.Incompatibilities[0].Counterexample)
		}
		if _, hasB := cex["b"]; hasB {
			t.Fatalf("counterexample should omit the new required field: %v", cex)
		}
		if _, hasA := cex["a"]; !hasA {
			t.Fatalf("counterexample should satisfy the old contract: %v", cex)
		}
	})

	t.Run("removed enum value is the example", func(t *testing.T) {
		res := Check(
			mustParse(t, `{"enum":["USD","EUR","CNY"]}`),
			mustParse(t, `{"enum":["USD","EUR"]}`),
			Request,
		)
		if len(res.Incompatibilities) != 1 {
			t.Fatalf("findings = %+v", res.Incompatibilities)
		}
		if res.Incompatibilities[0].Counterexample != "CNY" {
			t.Fatalf("counterexample = %v, want CNY", res.Incompatibilities[0].Counterexample)
		}
	})

	t.Run("raised minimum: example is below the new bound", func(t *testing.T) {
		res := Check(
			mustParse(t, `{"type":"number","minimum":0}`),
			mustParse(t, `{"type":"number","minimum":5}`),
			Request,
		)
		if len(res.Incompatibilities) != 1 {
			t.Fatalf("findings = %+v", res.Incompatibilities)
		}
		if res.Incompatibilities[0].Counterexample != float64(0) {
			t.Fatalf("counterexample = %v, want 0", res.Incompatibilities[0].Counterexample)
		}
	})

	t.Run("enum added where none existed: example is outside the enum", func(t *testing.T) {
		res := Check(
			mustParse(t, `{"type":"string"}`),
			mustParse(t, `{"type":"string","enum":["a","b"]}`),
			Request,
		)
		if len(res.Incompatibilities) != 1 {
			t.Fatalf("findings = %+v", res.Incompatibilities)
		}
		if res.Incompatibilities[0].Counterexample == "a" || res.Incompatibilities[0].Counterexample == "b" {
			t.Fatalf("counterexample must be outside the enum: %v", res.Incompatibilities[0].Counterexample)
		}
	})
}
