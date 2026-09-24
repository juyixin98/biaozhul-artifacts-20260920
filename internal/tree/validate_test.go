package tree

import "testing"

func TestParseAndValidateOK(t *testing.T) {
	raw := []byte(`{
      "root":"r",
      "nodes":{
        "r":{"id":"r","kind":"sequence","children":["p"]},
        "p":{"id":"p","kind":"parallel","success_threshold":1,"failure_threshold":1,"children":["a","b"]},
        "a":{"id":"a","kind":"timeout","timeout_ms":100,"children":["act"]},
        "act":{"id":"act","kind":"action","action":"stub"},
        "b":{"id":"b","kind":"action","action":"hash.sha256","params":{"input":"x"}}
      }}`)
	d, err := ParseDefinition(raw)
	if err != nil {
		t.Fatal(err)
	}
	if err := d.Validate(nil); err != nil {
		t.Fatalf("validate: %v", err)
	}
}

func TestValidateErrors(t *testing.T) {
	cases := []struct {
		name string
		raw  string
	}{
		{"empty nodes", `{"root":"r","nodes":{}}`},
		{"missing root", `{"root":"x","nodes":{"r":{"id":"r","kind":"action","action":"stub"}}}`},
		{"unknown kind", `{"root":"r","nodes":{"r":{"id":"r","kind":"nope"}}}`},
		{"sequence no children", `{"root":"r","nodes":{"r":{"id":"r","kind":"sequence"}}}`},
		{"timeout bad child count", `{"root":"r","nodes":{"r":{"id":"r","kind":"timeout","timeout_ms":10,"children":[]}}}`},
		{"timeout zero", `{"root":"r","nodes":{"r":{"id":"r","kind":"timeout","timeout_ms":0,"children":["a"]},"a":{"id":"a","kind":"action","action":"stub"}}}`},
		{"action missing name", `{"root":"r","nodes":{"r":{"id":"r","kind":"action"}}}`},
		{"unknown action", `{"root":"r","nodes":{"r":{"id":"r","kind":"action","action":"nope"}}}`},
		{"missing child", `{"root":"r","nodes":{"r":{"id":"r","kind":"sequence","children":["a"]}}}`},
		{"duplicate child", `{"root":"r","nodes":{"r":{"id":"r","kind":"sequence","children":["a","a"]},"a":{"id":"a","kind":"action","action":"stub"}}}`},
		{"cycle/shared node", `{"root":"r","nodes":{"r":{"id":"r","kind":"sequence","children":["a","b"]},"a":{"id":"a","kind":"sequence","children":["c"]},"b":{"id":"b","kind":"sequence","children":["c"]},"c":{"id":"c","kind":"action","action":"stub"}}}`},
		{"orphan node", `{"root":"r","nodes":{"r":{"id":"r","kind":"action","action":"stub"},"o":{"id":"o","kind":"action","action":"stub"}}}`},
		{"bad parallel thresholds zero", `{"root":"r","nodes":{"r":{"id":"r","kind":"parallel","success_threshold":0,"failure_threshold":1,"children":["a"]},"a":{"id":"a","kind":"action","action":"stub"}}}`},
		{"parallel threshold too big", `{"root":"r","nodes":{"r":{"id":"r","kind":"parallel","success_threshold":3,"failure_threshold":1,"children":["a"]},"a":{"id":"a","kind":"action","action":"stub"}}}`},
		{"parallel thresholds overlap too far", `{"root":"r","nodes":{"r":{"id":"r","kind":"parallel","success_threshold":2,"failure_threshold":3,"children":["a","b","c"]},"a":{"id":"a","kind":"action","action":"stub"},"b":{"id":"b","kind":"action","action":"stub"},"c":{"id":"c","kind":"action","action":"stub"}}}`},
	}
	known := func(name string) bool { return name == "stub" || name == "hash.sha256" }
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			d, err := ParseDefinition([]byte(tc.raw))
			if err != nil {
				t.Fatalf("parse: %v", err)
			}
			if err := d.Validate(known); err == nil {
				t.Fatalf("expected validation error for %s", tc.name)
			}
		})
	}
}

func TestContentHashStableAndSensitive(t *testing.T) {
	raw1 := []byte(`{
      "root":"r",
      "nodes":{
        "b":{"id":"b","kind":"action","action":"stub","params":{"y":2,"x":1}},
        "r":{"id":"r","kind":"sequence","children":["b"]}
      }}`)
	// Same semantic content, different JSON whitespace and map/order layout.
	raw2 := []byte(`{"nodes":{"r":{"children":["b"],"id":"r","kind":"sequence"},"b":{"params":{"x":1,"y":2},"id":"b","kind":"action","action":"stub"}},"root":"r"}`)
	d1, _ := ParseDefinition(raw1)
	d2, _ := ParseDefinition(raw2)
	h1, err := d1.ContentHash()
	if err != nil {
		t.Fatal(err)
	}
	h2, _ := d2.ContentHash()
	if h1 != h2 {
		t.Fatalf("semantically equal definitions hashed differently:\n%s\n%s", h1, h2)
	}

	// A real semantic change must change the hash.
	raw3 := []byte(`{"root":"r","nodes":{"r":{"id":"r","kind":"sequence","children":["b"]},"b":{"id":"b","kind":"action","action":"stub","params":{"x":1,"y":3}}}}`)
	d3, _ := ParseDefinition(raw3)
	h3, _ := d3.ContentHash()
	if h3 == h1 {
		t.Fatal("changed param produced identical hash")
	}
	if len(h1) != 64 {
		t.Fatalf("hash length=%d want 64 hex chars", len(h1))
	}
}

func TestCloneIndependent(t *testing.T) {
	d, _ := ParseDefinition([]byte(`{"root":"r","nodes":{"r":{"id":"r","kind":"action","action":"stub","params":{"a":[1,2]}}}}`))
	c := d.Clone()
	c.Nodes["r"].Params["a"].([]any)[0] = 99
	// JSON numbers decode as float64, so compare against float64(1).
	if d.Nodes["r"].Params["a"].([]any)[0] != float64(1) {
		t.Fatalf("clone shares mutable params with original: %v", d.Nodes["r"].Params["a"])
	}
}
