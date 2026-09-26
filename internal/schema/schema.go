// Package schema parses a deliberately small subset of JSON Schema.
//
// Supported keywords: type, properties, required, enum, items,
// minimum, maximum, exclusiveMinimum, exclusiveMaximum.
// Annotation keywords ($schema, title, description, default, examples)
// are parsed and ignored. Any other keyword is recorded in
// Schema.Unsupported so callers can report "unknown" instead of
// silently treating the contract as compatible.
package schema

import (
	"encoding/json"
	"fmt"
	"sort"
)

var knownKeywords = map[string]bool{
	"type":             true,
	"properties":       true,
	"required":         true,
	"enum":             true,
	"items":            true,
	"minimum":          true,
	"maximum":          true,
	"exclusiveMinimum": true,
	"exclusiveMaximum": true,
}

var ignoredKeywords = map[string]bool{
	"$schema":     true,
	"title":       true,
	"description": true,
	"default":     true,
	"examples":    true,
}

// Schema is the parsed form of the supported JSON Schema subset.
type Schema struct {
	Types            []string
	Enum             []any
	Properties       map[string]*Schema
	Required         []string
	Items            *Schema
	Minimum          *float64
	Maximum          *float64
	ExclusiveMinimum *float64
	ExclusiveMaximum *float64

	// Unsupported lists keywords present in the source document that this
	// subset does not implement (e.g. pattern, format, additionalProperties).
	Unsupported []string
}

// Parse decodes a JSON schema document.
func Parse(data []byte) (*Schema, error) {
	var raw any
	if err := json.Unmarshal(data, &raw); err != nil {
		return nil, fmt.Errorf("schema: invalid JSON: %w", err)
	}
	return parseNode(raw)
}

func parseNode(raw any) (*Schema, error) {
	obj, ok := raw.(map[string]any)
	if !ok {
		return nil, fmt.Errorf("schema: node must be a JSON object, got %T", raw)
	}
	s := &Schema{}
	for key, val := range obj {
		switch {
		case knownKeywords[key]:
			if err := applyKnown(s, key, val); err != nil {
				return nil, err
			}
		case ignoredKeywords[key]:
			// annotation only, no validation semantics
		default:
			s.Unsupported = append(s.Unsupported, key)
		}
	}
	sort.Strings(s.Unsupported)
	return s, nil
}

func applyKnown(s *Schema, key string, val any) error {
	switch key {
	case "type":
		types, err := parseTypes(val)
		if err != nil {
			return err
		}
		s.Types = types
	case "enum":
		arr, ok := val.([]any)
		if !ok {
			return fmt.Errorf("schema: enum must be an array")
		}
		s.Enum = arr
	case "required":
		arr, ok := val.([]any)
		if !ok {
			return fmt.Errorf("schema: required must be an array")
		}
		for _, item := range arr {
			name, ok := item.(string)
			if !ok {
				return fmt.Errorf("schema: required entries must be strings")
			}
			s.Required = append(s.Required, name)
		}
	case "properties":
		obj, ok := val.(map[string]any)
		if !ok {
			return fmt.Errorf("schema: properties must be an object")
		}
		s.Properties = make(map[string]*Schema, len(obj))
		for name, raw := range obj {
			child, err := parseNode(raw)
			if err != nil {
				return fmt.Errorf("schema: properties.%s: %w", name, err)
			}
			s.Properties[name] = child
		}
	case "items":
		child, err := parseNode(val)
		if err != nil {
			return fmt.Errorf("schema: items: %w", err)
		}
		s.Items = child
	case "minimum":
		f, err := toFloat(val, key)
		if err != nil {
			return err
		}
		s.Minimum = &f
	case "maximum":
		f, err := toFloat(val, key)
		if err != nil {
			return err
		}
		s.Maximum = &f
	case "exclusiveMinimum":
		f, err := toFloat(val, key)
		if err != nil {
			return err
		}
		s.ExclusiveMinimum = &f
	case "exclusiveMaximum":
		f, err := toFloat(val, key)
		if err != nil {
			return err
		}
		s.ExclusiveMaximum = &f
	}
	return nil
}

func parseTypes(val any) ([]string, error) {
	switch v := val.(type) {
	case string:
		return []string{v}, nil
	case []any:
		out := make([]string, 0, len(v))
		for _, item := range v {
			name, ok := item.(string)
			if !ok {
				return nil, fmt.Errorf("schema: type array entries must be strings")
			}
			out = append(out, name)
		}
		return out, nil
	default:
		return nil, fmt.Errorf("schema: type must be a string or array of strings")
	}
}

func toFloat(val any, key string) (float64, error) {
	f, ok := val.(float64)
	if !ok {
		return 0, fmt.Errorf("schema: %s must be a number", key)
	}
	return f, nil
}
