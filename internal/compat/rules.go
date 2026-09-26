package compat

import (
	"fmt"
	"reflect"

	"contractcheck/internal/schema"
)

var allTypes = []string{"string", "integer", "number", "boolean", "array", "object", "null"}

// reason picks a direction-aware message: in the request direction "sub" is
// the old contract and "sup" the new one; in the response direction it is
// the other way around.
func (c *checker) reason(requestMsg, responseMsg string) string {
	if c.dir == Response {
		return responseMsg
	}
	return requestMsg
}

func (c *checker) checkTypes(sub, sup *schema.Schema, path string) {
	if len(sup.Types) == 0 {
		return
	}
	allowed := make(map[string]bool, len(sup.Types))
	for _, t := range sup.Types {
		allowed[t] = true
	}
	if len(sub.Types) == 0 {
		// sub accepts any type; find one sup rejects.
		for _, t := range allTypes {
			if !allowed[t] {
				c.finding(path, c.reason(
					fmt.Sprintf("new contract only accepts type %v, old contract accepted any type", sup.Types),
					fmt.Sprintf("new contract may return any type, old contract only allowed %v", sup.Types),
				), zeroValue(t))
				return
			}
		}
		return
	}
	for _, t := range sub.Types {
		if !allowed[t] {
			c.finding(path, c.reason(
				fmt.Sprintf("type %q is no longer accepted (allowed: %v)", t, sup.Types),
				fmt.Sprintf("new contract may return type %q, which the old contract did not allow (allowed: %v)", t, sup.Types),
			), zeroValue(t))
		}
	}
}

func (c *checker) checkEnum(sub, sup *schema.Schema, path string) {
	if len(sup.Enum) == 0 {
		return
	}
	if len(sub.Enum) > 0 {
		for _, v := range sub.Enum {
			if !enumContains(sup.Enum, v) {
				c.finding(path, c.reason(
					fmt.Sprintf("enum value %v was removed by the new contract", v),
					fmt.Sprintf("new contract may return enum value %v, which the old contract did not allow", v),
				), v)
			}
		}
		return
	}
	// sub is unconstrained: produce a value of sub's type outside sup's enum.
	if v, ok := valueOutsideEnum(sub.Types, sup.Enum); ok {
		c.finding(path, c.reason(
			"new contract restricts values to an enum the old contract did not have",
			"new contract may return values outside the old contract's enum",
		), v)
	}
}

func (c *checker) checkRange(sub, sup *schema.Schema, path string) {
	if bound, exclusive, ok := lowerBound(sup); ok {
		subBound, subExclusive, hasSub := lowerBound(sub)
		switch {
		case !hasSub:
			c.finding(path, c.reason(
				fmt.Sprintf("new contract adds a lower bound (%s %v)", boundName(exclusive), bound),
				fmt.Sprintf("new contract may return values below the old lower bound (%s %v)", boundName(exclusive), bound),
			), bound-1)
		case subBound < bound:
			c.finding(path, c.reason(
				fmt.Sprintf("new contract raises the lower bound to %s %v", boundName(exclusive), bound),
				fmt.Sprintf("new contract may return values below the old lower bound (%s %v)", boundName(exclusive), bound),
			), subBound)
		case subBound == bound && exclusive && !subExclusive:
			c.finding(path, c.reason(
				"new contract makes the lower bound exclusive",
				"new contract may return the boundary value, which the old exclusive lower bound rejected",
			), subBound)
		}
	}
	if bound, exclusive, ok := upperBound(sup); ok {
		subBound, subExclusive, hasSub := upperBound(sub)
		switch {
		case !hasSub:
			c.finding(path, c.reason(
				fmt.Sprintf("new contract adds an upper bound (%s %v)", boundName(exclusive), bound),
				fmt.Sprintf("new contract may return values above the old upper bound (%s %v)", boundName(exclusive), bound),
			), bound+1)
		case subBound > bound:
			c.finding(path, c.reason(
				fmt.Sprintf("new contract lowers the upper bound to %s %v", boundName(exclusive), bound),
				fmt.Sprintf("new contract may return values above the old upper bound (%s %v)", boundName(exclusive), bound),
			), subBound)
		case subBound == bound && exclusive && !subExclusive:
			c.finding(path, c.reason(
				"new contract makes the upper bound exclusive",
				"new contract may return the boundary value, which the old exclusive upper bound rejected",
			), subBound)
		}
	}
}

func (c *checker) checkObject(sub, sup *schema.Schema, path string) {
	subRequired := make(map[string]bool, len(sub.Required))
	for _, r := range sub.Required {
		subRequired[r] = true
	}
	for _, r := range sup.Required {
		if subRequired[r] {
			continue
		}
		// Counterexample: an object satisfying the "sub" contract that omits
		// the field the "sup" side requires.
		cex := map[string]any{}
		for _, k := range sub.Required {
			cex[k] = defaultValue(sub.Properties[k])
		}
		c.finding(path, c.reason(
			fmt.Sprintf("field %q is now required", r),
			fmt.Sprintf("field %q is no longer guaranteed in responses", r),
		), cex)
	}
	for name, supProp := range sup.Properties {
		subProp, ok := sub.Properties[name]
		if !ok {
			// The sub schema does not constrain this property; only a
			// required-field change (handled above) can break compatibility.
			continue
		}
		c.subset(subProp, supProp, path+"."+name)
	}
}

func (c *checker) checkItems(sub, sup *schema.Schema, path string) {
	if sub.Items != nil && sup.Items != nil {
		c.subset(sub.Items, sup.Items, path+"[*]")
	}
}

// lowerBound returns the effective lower bound and whether it is exclusive.
func lowerBound(s *schema.Schema) (bound float64, exclusive, ok bool) {
	switch {
	case s.ExclusiveMinimum != nil && s.Minimum != nil:
		if *s.ExclusiveMinimum >= *s.Minimum {
			return *s.ExclusiveMinimum, true, true
		}
		return *s.Minimum, false, true
	case s.ExclusiveMinimum != nil:
		return *s.ExclusiveMinimum, true, true
	case s.Minimum != nil:
		return *s.Minimum, false, true
	}
	return 0, false, false
}

// upperBound returns the effective upper bound and whether it is exclusive.
func upperBound(s *schema.Schema) (bound float64, exclusive, ok bool) {
	switch {
	case s.ExclusiveMaximum != nil && s.Maximum != nil:
		if *s.ExclusiveMaximum <= *s.Maximum {
			return *s.ExclusiveMaximum, true, true
		}
		return *s.Maximum, false, true
	case s.ExclusiveMaximum != nil:
		return *s.ExclusiveMaximum, true, true
	case s.Maximum != nil:
		return *s.Maximum, false, true
	}
	return 0, false, false
}

func boundName(exclusive bool) string {
	if exclusive {
		return "exclusive"
	}
	return "inclusive"
}

func enumContains(enum []any, v any) bool {
	for _, e := range enum {
		if reflect.DeepEqual(e, v) {
			return true
		}
	}
	return false
}

// valueOutsideEnum constructs a value of one of the given types that is not
// present in enum. Returns ok=false when no candidate works.
func valueOutsideEnum(types []string, enum []any) (any, bool) {
	candidates := []any{"__outside_enum__", float64(0), float64(1), float64(-1), 0.5, true, false}
	if len(types) > 0 {
		candidates = nil
		for _, t := range types {
			switch t {
			case "string":
				candidates = append(candidates, "__outside_enum__")
			case "integer":
				candidates = append(candidates, float64(0), float64(1), float64(-1), float64(2))
			case "number":
				candidates = append(candidates, 0.5, 1.5, -0.5)
			case "boolean":
				candidates = append(candidates, true, false)
			}
		}
	}
	for _, cand := range candidates {
		if !enumContains(enum, cand) {
			return cand, true
		}
	}
	return nil, false
}

func zeroValue(typ string) any {
	switch typ {
	case "string":
		return "string"
	case "integer":
		return float64(0)
	case "number":
		return 0.5
	case "boolean":
		return true
	case "array":
		return []any{}
	case "object":
		return map[string]any{}
	case "null":
		return nil
	}
	return nil
}

// defaultValue builds a representative value valid under s, used to fill
// counterexample objects.
func defaultValue(s *schema.Schema) any {
	if s == nil {
		return "value"
	}
	if len(s.Enum) > 0 {
		return s.Enum[0]
	}
	if len(s.Types) > 0 {
		return zeroValue(s.Types[0])
	}
	return "value"
}
