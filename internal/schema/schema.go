// Package schema 实现一个有限的 JSON Schema 子集：
// 解析、规范化、示例值生成，以及“不支持关键字”探测。
//
// 支持的约束关键字：
//
//	type(含 string/array 形式)、enum、properties、required、
//	minimum、maximum、minLength、maxLength、items、additionalProperties
//
// 已知但不产生约束、安全忽略的注解关键字：
//
//	$schema、title、description、default、examples
//
// 其余任何关键字（$ref、const、format、oneOf、allOf、pattern、multipleOf 等）
// 都会被记录为“未知关键字”，兼容性结论因此只能是 unknown，绝不判为通过。
package schema

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"sort"
)

// 支持的基础类型。integer 是 number 的子类型。
const (
	TypeString  = "string"
	TypeNumber  = "number"
	TypeInteger = "integer"
	TypeBoolean = "boolean"
	TypeObject  = "object"
	TypeArray   = "array"
	TypeNull    = "null"
)

var knownTypes = map[string]bool{
	TypeString: true, TypeNumber: true, TypeInteger: true,
	TypeBoolean: true, TypeObject: true, TypeArray: true, TypeNull: true,
}

// annotationKeywords 是已知的注解/元信息关键字，不约束实例，可安全忽略。
var annotationKeywords = map[string]bool{
	"$schema": true, "title": true, "description": true,
	"default": true, "examples": true,
}

// Schema 是子集内一个 JSON Schema 节点的规范化表示。
type Schema struct {
	// Bool 非 nil 时表示 JSON 布尔模式：true 接受一切，false 拒绝一切。
	Bool *bool

	HasType bool
	Type    []string

	HasEnum bool
	// Enum 保留规范化后的枚举值（JSON 数字解码为 float64）。
	Enum []any

	Properties map[string]*Schema
	Required   []string

	Minimum   *float64
	Maximum   *float64
	MinLength *int
	MaxLength *int

	Items *Schema

	HasAdditionalProps bool
	AdditionalProps    *Schema

	// Unknown 收集本子树内出现的所有不支持关键字，路径为 JSON Pointer 风格，
	// 例如 "/properties/age/exclusiveMinimum"。
	Unknown []UnknownKeyword
}

// UnknownKeyword 描述一个出现在某路径上的、本实现不支持的 schema 关键字。
type UnknownKeyword struct {
	Path    string `json:"path"`
	Keyword string `json:"keyword"`
}

// Parse 从 r 读取 JSON 并解析 schema。
func Parse(r io.Reader) (*Schema, error) {
	var raw json.RawMessage
	dec := json.NewDecoder(r)
	if err := dec.Decode(&raw); err != nil {
		return nil, fmt.Errorf("schema 不是合法 JSON: %w", err)
	}
	return parseNode(raw, "")
}

// ParseBytes 解析 JSON 字节形式的 schema。
func ParseBytes(b []byte) (*Schema, error) {
	return Parse(bytes.NewReader(b))
}

func parseNode(raw json.RawMessage, pointer string) (*Schema, error) {
	trimmed := bytes.TrimSpace(raw)
	if len(trimmed) == 0 {
		return nil, fmt.Errorf("路径 %q 处 schema 为空", pointer)
	}
	// 布尔模式：true / false。
	if trimmed[0] == 't' || trimmed[0] == 'f' {
		var b bool
		if err := json.Unmarshal(trimmed, &b); err != nil {
			return nil, fmt.Errorf("路径 %q 处布尔 schema 非法: %w", pointer, err)
		}
		return &Schema{Bool: &b}, nil
	}
	if trimmed[0] != '{' {
		return nil, fmt.Errorf("路径 %q 处 schema 必须是对象或布尔值", pointer)
	}

	var m map[string]json.RawMessage
	if err := json.Unmarshal(trimmed, &m); err != nil {
		return nil, fmt.Errorf("路径 %q 处 schema 对象非法: %w", pointer, err)
	}

	s := &Schema{}
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	sort.Strings(keys)

	for _, k := range keys {
		v := m[k]
		switch k {
		case "type":
			if err := parseType(v, s); err != nil {
				return nil, err
			}
		case "enum":
			if err := parseEnum(v, s); err != nil {
				return nil, err
			}
		case "required":
			if err := parseRequired(v, s); err != nil {
				return nil, err
			}
		case "properties":
			if err := parseProperties(v, pointer, s); err != nil {
				return nil, err
			}
		case "items":
			sub, err := parseNode(v, pointer+"/items")
			if err != nil {
				return nil, err
			}
			s.Items = sub
		case "additionalProperties":
			s.HasAdditionalProps = true
			sub, err := parseNode(v, pointer+"/additionalProperties")
			if err != nil {
				return nil, err
			}
			s.AdditionalProps = sub
		case "minimum", "maximum":
			n, err := parseNumber(v)
			if err != nil {
				return nil, fmt.Errorf("路径 %q 处 %s: %w", pointer, k, err)
			}
			if k == "minimum" {
				s.Minimum = &n
			} else {
				s.Maximum = &n
			}
		case "minLength", "maxLength":
			n, err := parseInt(v)
			if err != nil {
				return nil, fmt.Errorf("路径 %q 处 %s: %w", pointer, k, err)
			}
			if n < 0 {
				return nil, fmt.Errorf("路径 %q 处 %s 不能为负数", pointer, k)
			}
			if k == "minLength" {
				s.MinLength = &n
			} else {
				s.MaxLength = &n
			}
		default:
			if annotationKeywords[k] {
				continue
			}
			s.Unknown = append(s.Unknown, UnknownKeyword{
				Path:    pointer,
				Keyword: k,
			})
		}
	}

	// 收集子 schema 中的未知关键字。
	for _, name := range sortedPropertyNames(s.Properties) {
		s.Unknown = append(s.Unknown, s.Properties[name].Unknown...)
	}
	if s.Items != nil {
		s.Unknown = append(s.Unknown, s.Items.Unknown...)
	}
	if s.HasAdditionalProps && s.AdditionalProps != nil {
		s.Unknown = append(s.Unknown, s.AdditionalProps.Unknown...)
	}
	return s, nil
}

func parseType(raw json.RawMessage, s *Schema) error {
	trimmed := bytes.TrimSpace(raw)
	if len(trimmed) > 0 && trimmed[0] == '[' {
		var arr []string
		if err := json.Unmarshal(raw, &arr); err != nil {
			return fmt.Errorf("type 必须是字符串或字符串数组: %w", err)
		}
		if len(arr) == 0 {
			return fmt.Errorf("type 数组不能为空")
		}
		for _, t := range arr {
			if !knownTypes[t] {
				return fmt.Errorf("不支持的类型 %q", t)
			}
		}
		s.Type = arr
	} else {
		var t string
		if err := json.Unmarshal(raw, &t); err != nil {
			return fmt.Errorf("type 必须是字符串或字符串数组: %w", err)
		}
		if !knownTypes[t] {
			return fmt.Errorf("不支持的类型 %q", t)
		}
		s.Type = []string{t}
	}
	s.HasType = true
	return nil
}

func parseEnum(raw json.RawMessage, s *Schema) error {
	var raws []json.RawMessage
	if err := json.Unmarshal(raw, &raws); err != nil {
		return fmt.Errorf("enum 必须是数组: %w", err)
	}
	if len(raws) == 0 {
		return fmt.Errorf("enum 数组不能为空")
	}
	vals := make([]any, 0, len(raws))
	for _, item := range raws {
		var v any
		if err := json.Unmarshal(item, &v); err != nil {
			return fmt.Errorf("enum 元素不是合法 JSON 值: %w", err)
		}
		vals = append(vals, canonical(v))
	}
	s.HasEnum = true
	s.Enum = vals
	return nil
}

func parseRequired(raw json.RawMessage, s *Schema) error {
	var reqs []string
	if err := json.Unmarshal(raw, &reqs); err != nil {
		return fmt.Errorf("required 必须是字符串数组: %w", err)
	}
	seen := map[string]bool{}
	for _, r := range reqs {
		if r == "" {
			return fmt.Errorf("required 不能包含空字符串")
		}
		if seen[r] {
			return fmt.Errorf("required 包含重复字段 %q", r)
		}
		seen[r] = true
	}
	s.Required = reqs
	return nil
}

func parseProperties(raw json.RawMessage, pointer string, s *Schema) error {
	var props map[string]json.RawMessage
	if err := json.Unmarshal(raw, &props); err != nil {
		return fmt.Errorf("properties 必须是对象: %w", err)
	}
	s.Properties = map[string]*Schema{}
	for _, name := range sortedMapKeys(props) {
		sub, err := parseNode(props[name], pointer+"/properties/"+name)
		if err != nil {
			return err
		}
		s.Properties[name] = sub
	}
	return nil
}

func parseNumber(raw json.RawMessage) (float64, error) {
	var n float64
	if err := json.Unmarshal(raw, &n); err != nil {
		return 0, fmt.Errorf("必须是数字: %w", err)
	}
	return n, nil
}

func parseInt(raw json.RawMessage) (int, error) {
	var n json.Number
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.UseNumber()
	if err := dec.Decode(&n); err != nil {
		return 0, fmt.Errorf("必须是整数: %w", err)
	}
	i, err := n.Int64()
	if err != nil {
		return 0, fmt.Errorf("必须是整数: %w", err)
	}
	return int(i), nil
}

// canonical 将 JSON 解码值规范化，保证枚举比较时数值键一致。
func canonical(v any) any {
	switch t := v.(type) {
	case map[string]any:
		out := make(map[string]any, len(t))
		for k, sub := range t {
			out[k] = canonical(sub)
		}
		return out
	case []any:
		out := make([]any, len(t))
		for i := range t {
			out[i] = canonical(t[i])
		}
		return out
	default:
		return v
	}
}

func sortedMapKeys(m map[string]json.RawMessage) []string {
	names := make([]string, 0, len(m))
	for k := range m {
		names = append(names, k)
	}
	sort.Strings(names)
	return names
}

func sortedPropertyNames(m map[string]*Schema) []string {
	names := make([]string, 0, len(m))
	for k := range m {
		names = append(names, k)
	}
	sort.Strings(names)
	return names
}
