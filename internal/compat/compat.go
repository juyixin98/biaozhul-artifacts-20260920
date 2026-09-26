// Package compat decides whether a new contract is compatible with an old
// one, per direction:
//
//   - request:  every request valid under the OLD schema must still be valid
//     under the NEW schema (old ⊆ new). Relaxing requests is compatible.
//   - response: every response valid under the NEW schema must have been
//     valid under the OLD schema (new ⊆ old). Tightening responses is
//     compatible.
//
// Findings include a JSON path and a concrete counterexample value: a value
// accepted by the "sub" side but rejected by the "sup" side. Schemas using
// keywords outside the supported subset yield status "unknown", never a pass.
package compat

import (
	"contractcheck/internal/schema"
)

// Direction selects which side of the contract is being checked.
type Direction string

const (
	Request  Direction = "request"
	Response Direction = "response"
)

// Status values for Result.Status.
const (
	StatusCompatible   = "compatible"
	StatusIncompatible = "incompatible"
	StatusUnknown      = "unknown"
)

// Finding is one concrete incompatibility.
type Finding struct {
	Path           string `json:"path"`
	Reason         string `json:"reason"`
	Counterexample any    `json:"counterexample,omitempty"`
}

// Unknown marks a schema node that uses keywords outside the supported subset.
type Unknown struct {
	Path    string `json:"path"`
	Keyword string `json:"keyword"`
}

// Result is the structured outcome of one directional check.
type Result struct {
	Direction         Direction `json:"direction"`
	Status            string    `json:"status"`
	Incompatibilities []Finding `json:"incompatibilities,omitempty"`
	Unknowns          []Unknown `json:"unknowns,omitempty"`
}

// Check compares old and new schemas for the given direction.
func Check(old, new *schema.Schema, dir Direction) Result {
	sub, sup := old, new
	if dir == Response {
		sub, sup = new, old
	}
	c := &checker{dir: dir}
	c.subset(sub, sup, "$")

	res := Result{
		Direction:         dir,
		Status:            StatusCompatible,
		Incompatibilities: c.findings,
		Unknowns:          c.unknowns,
	}
	if len(res.Unknowns) > 0 {
		res.Status = StatusUnknown
	}
	if len(res.Incompatibilities) > 0 {
		res.Status = StatusIncompatible
	}
	return res
}

type checker struct {
	dir      Direction
	findings []Finding
	unknowns []Unknown
}

func (c *checker) finding(path, reason string, counterexample any) {
	c.findings = append(c.findings, Finding{Path: path, Reason: reason, Counterexample: counterexample})
}

func (c *checker) unknown(path, keyword string) {
	c.unknowns = append(c.unknowns, Unknown{Path: path, Keyword: keyword})
}

// subset verifies that every value valid under sub is also valid under sup,
// recording one finding per violated constraint.
func (c *checker) subset(sub, sup *schema.Schema, path string) {
	for _, k := range sub.Unsupported {
		c.unknown(path, k)
	}
	for _, k := range sup.Unsupported {
		c.unknown(path, k)
	}
	c.checkTypes(sub, sup, path)
	c.checkEnum(sub, sup, path)
	c.checkRange(sub, sup, path)
	c.checkObject(sub, sup, path)
	c.checkItems(sub, sup, path)
}
