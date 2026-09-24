package attest

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"regexp"
	"sort"
)

// canonicalInt 是唯一被接受的 JSON 数值形式：整数、无前导零、无 -0、
// 无小数点、无指数。其它写法（1.0、01、1e3、-0 …）一律视为非规范而拒绝。
var canonicalInt = regexp.MustCompile(`^(0|-?[1-9][0-9]*)$`)

// ParseStrict 以严格模式解析 JSON：
//   - 拒绝对象中的重复键；
//   - 拒绝非规范数值（见 canonicalInt）；
//   - 拒绝顶层值之后的任何尾随数据。
//
// 返回的值只包含 map[string]any、[]any、string、bool、nil、json.Number。
func ParseStrict(data []byte) (any, error) {
	dec := json.NewDecoder(bytes.NewReader(data))
	dec.UseNumber()
	v, err := parseValue(dec)
	if err != nil {
		return nil, err
	}
	if _, err := dec.Token(); err != io.EOF {
		return nil, fmt.Errorf("顶层 JSON 值之后存在尾随数据")
	}
	return v, nil
}

func parseValue(dec *json.Decoder) (any, error) {
	tok, err := dec.Token()
	if err != nil {
		return nil, err
	}
	switch t := tok.(type) {
	case json.Delim:
		switch t {
		case '{':
			obj := make(map[string]any)
			for dec.More() {
				ktok, err := dec.Token()
				if err != nil {
					return nil, err
				}
				key, ok := ktok.(string)
				if !ok {
					return nil, fmt.Errorf("对象键不是字符串")
				}
				if _, dup := obj[key]; dup {
					return nil, fmt.Errorf("对象中存在重复键 %q", key)
				}
				val, err := parseValue(dec)
				if err != nil {
					return nil, err
				}
				obj[key] = val
			}
			if _, err := dec.Token(); err != nil { // 读取闭合 '}'
				return nil, err
			}
			return obj, nil
		case '[':
			var arr []any
			for dec.More() {
				v, err := parseValue(dec)
				if err != nil {
					return nil, err
				}
				arr = append(arr, v)
			}
			if _, err := dec.Token(); err != nil { // 读取闭合 ']'
				return nil, err
			}
			return arr, nil
		}
		return nil, fmt.Errorf("意外的分隔符 %q", string(t))
	case json.Number:
		s := t.String()
		if !canonicalInt.MatchString(s) {
			return nil, fmt.Errorf("非规范数值 %q：仅允许无前导零的整数", s)
		}
		return t, nil
	case string, bool, nil:
		return t, nil
	default:
		return nil, fmt.Errorf("意外的 JSON token 类型 %T", tok)
	}
}

// Canonical 把 ParseStrict 返回的值序列化为规范 JSON：
// 对象键按字典序排列、无空白、字符串最小转义（不做 HTML 转义）。
// 同一逻辑值永远得到同一字节串，因此可直接作为签名输入。
func Canonical(v any) ([]byte, error) {
	var buf bytes.Buffer
	if err := writeCanonical(&buf, v); err != nil {
		return nil, err
	}
	return buf.Bytes(), nil
}

func writeCanonical(buf *bytes.Buffer, v any) error {
	switch t := v.(type) {
	case map[string]any:
		keys := make([]string, 0, len(t))
		for k := range t {
			keys = append(keys, k)
		}
		sort.Strings(keys)
		buf.WriteByte('{')
		for i, k := range keys {
			if i > 0 {
				buf.WriteByte(',')
			}
			writeJSONString(buf, k)
			buf.WriteByte(':')
			if err := writeCanonical(buf, t[k]); err != nil {
				return err
			}
		}
		buf.WriteByte('}')
	case []any:
		buf.WriteByte('[')
		for i, e := range t {
			if i > 0 {
				buf.WriteByte(',')
			}
			if err := writeCanonical(buf, e); err != nil {
				return err
			}
		}
		buf.WriteByte(']')
	case string:
		writeJSONString(buf, t)
	case json.Number:
		buf.WriteString(t.String())
	case bool:
		if t {
			buf.WriteString("true")
		} else {
			buf.WriteString("false")
		}
	case nil:
		buf.WriteString("null")
	default:
		return fmt.Errorf("无法规范化的类型 %T", v)
	}
	return nil
}

func writeJSONString(buf *bytes.Buffer, s string) {
	buf.WriteByte('"')
	for _, r := range s {
		switch r {
		case '"':
			buf.WriteString(`\"`)
		case '\\':
			buf.WriteString(`\\`)
		case '\n':
			buf.WriteString(`\n`)
		case '\r':
			buf.WriteString(`\r`)
		case '\t':
			buf.WriteString(`\t`)
		case '\b':
			buf.WriteString(`\b`)
		case '\f':
			buf.WriteString(`\f`)
		default:
			if r < 0x20 {
				fmt.Fprintf(buf, `\u%04x`, r)
			} else {
				buf.WriteRune(r)
			}
		}
	}
	buf.WriteByte('"')
}
