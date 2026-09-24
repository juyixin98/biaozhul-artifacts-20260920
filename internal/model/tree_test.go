package model

import (
	"encoding/json"
	"testing"
)

func mustTree(t *testing.T, raw string) *Tree {
	t.Helper()
	var tr Tree
	if err := json.Unmarshal([]byte(raw), &tr); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	return &tr
}

func TestValidateAcceptsWellFormed(t *testing.T) {
	tr := mustTree(t, `{
      "name":"t",
      "root":{"id":"r","kind":"root","children":[
        {"id":"s","kind":"sequence","children":[
          {"id":"a","kind":"action","stub":"succeed","non_idempotent":true},
          {"id":"p","kind":"parallel","success":1,"failure":1,"children":[
            {"id":"p1","kind":"action","stub":"wait","args":{"ms":5}},
            {"id":"g","kind":"timeout","ms":100,"child":
              {"id":"g1","kind":"action","stub":"succeed"}}
          ]}
        ]}
      ]}
    }`)
	if err := tr.Validate(); err != nil {
		t.Fatalf("validate: %v", err)
	}
}

func TestValidateRejectsBadTrees(t *testing.T) {
	cases := map[string]string{
		"empty name":            `{"name":"","root":{"id":"r","kind":"root","children":[{"id":"a","kind":"action","stub":"x"}]}}`,
		"root wrong kind":       `{"name":"t","root":{"id":"r","kind":"sequence","children":[{"id":"a","kind":"action","stub":"x"}]}}`,
		"duplicate ids":         `{"name":"t","root":{"id":"x","kind":"root","children":[{"id":"x","kind":"action","stub":"x"}]}}`,
		"action missing stub":   `{"name":"t","root":{"id":"r","kind":"root","children":[{"id":"a","kind":"action"}]}}`,
		"timeout missing child": `{"name":"t","root":{"id":"r","kind":"root","children":[{"id":"to","kind":"timeout","ms":10}]}}`,
		"timeout zero ms":       `{"name":"t","root":{"id":"r","kind":"root","children":[{"id":"to","kind":"timeout","ms":0,"child":{"id":"a","kind":"action","stub":"x"}}]}}`,
		"unknown kind":          `{"name":"t","root":{"id":"r","kind":"root","children":[{"id":"a","kind":"wizard"}]}}`,
		"parallel undecidable":  `{"name":"t","root":{"id":"r","kind":"root","children":[{"id":"p","kind":"parallel","success":2,"failure":2,"children":[{"id":"a","kind":"action","stub":"x"},{"id":"b","kind":"action","stub":"y"}]}]}}`,
	}
	for name, raw := range cases {
		t.Run(name, func(t *testing.T) {
			if err := mustTree(t, raw).Validate(); err == nil {
				t.Fatalf("expected validation error for %s", name)
			}
		})
	}
}

func TestHashIsContentAddressed(t *testing.T) {
	a := mustTree(t, `{"name":"t","root":{"id":"r","kind":"root","children":[
	  {"id":"a","kind":"action","stub":"echo","args":{"z":1,"a":2}}]}}`)
	b := mustTree(t, "{\"name\":\"t\",\"root\":{\"id\":\"r\",\"kind\":\"root\",\"children\":[\n"+
		"  {\"id\":\"a\",\"kind\":\"action\",\"stub\":\"echo\",\"args\":{\"a\":2,\"z\":1}}]}}")
	ha, err := a.Hash()
	if err != nil {
		t.Fatal(err)
	}
	hb, err := b.Hash()
	if err != nil {
		t.Fatal(err)
	}
	if ha != hb {
		t.Fatalf("key ordering/whitespace changed hash: %s != %s", ha, hb)
	}

	c := mustTree(t, `{"name":"t","root":{"id":"r","kind":"root","children":[
	  {"id":"a","kind":"action","stub":"echo","args":{"z":1,"a":3}}]}}`)
	hc, _ := c.Hash()
	if hc == ha {
		t.Fatal("semantic change did not change hash")
	}
}

func TestParallelThresholdDefaults(t *testing.T) {
	n := &Node{Kind: KindParallel, Children: make([]*Node, 3)}
	s, f := n.ParallelThresholds(3)
	if s != 3 || f != 3 {
		t.Fatalf("defaults = (%d,%d), want (3,3)", s, f)
	}
}
