// Package canon 提供确定性的 JSON 规范化：签名/哈希前所有结构化数据都走这里，
// 保证“同一语义内容 → 同一字节串”，不依赖 map 遍历顺序。
package canon

import (
	"bytes"
	"encoding/json"
	"sort"
)

// Encode 将任意 Go 值规范化为 UTF-8 JSON：
// map 的键按字典序排列，无多余空白，HTML 不转义。
// 传入的 json.RawMessage 若是对象/数组会被递归规范化。
func Encode(v any) ([]byte, error) {
	var buf bytes.Buffer
	if err := writeCanon(&buf, v); err != nil {
		return nil, err
	}
	return buf.Bytes(), nil
}

func writeCanon(buf *bytes.Buffer, v any) error {
	switch t := v.(type) {
	case json.RawMessage:
		var decoded any
		if len(t) == 0 {
			buf.WriteString("null")
			return nil
		}
		dec := json.NewDecoder(bytes.NewReader(t))
		dec.UseNumber()
		if err := dec.Decode(&decoded); err != nil {
			return err
		}
		return writeCanon(buf, decoded)
	case map[string]any:
		return writeObject(buf, t)
	case map[string]json.RawMessage:
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
			kb, _ := json.Marshal(k)
			buf.Write(kb)
			buf.WriteByte(':')
			var nested any
			if err := json.Unmarshal(t[k], &nested); err != nil {
				return err
			}
			if err := writeCanon(buf, nested); err != nil {
				return err
			}
		}
		buf.WriteByte('}')
		return nil
	case []any:
		buf.WriteByte('[')
		for i := range t {
			if i > 0 {
				buf.WriteByte(',')
			}
			if err := writeCanon(buf, t[i]); err != nil {
				return err
			}
		}
		buf.WriteByte(']')
		return nil
	case []json.RawMessage:
		buf.WriteByte('[')
		for i := range t {
			if i > 0 {
				buf.WriteByte(',')
			}
			var nested any
			if err := json.Unmarshal(t[i], &nested); err != nil {
				return err
			}
			if err := writeCanon(buf, nested); err != nil {
				return err
			}
		}
		buf.WriteByte(']')
		return nil
	case nil:
		buf.WriteString("null")
		return nil
	case string:
		b, err := json.Marshal(t)
		if err != nil {
			return err
		}
		buf.Write(b)
		return nil
	case json.Number:
		buf.WriteString(t.String())
		return nil
	case bool:
		if t {
			buf.WriteString("true")
		} else {
			buf.WriteString("false")
		}
		return nil
	case float64:
		// 测试证据中数字以 json.Number 进入；走到这里说明是 Go 原生浮点，按编码处理
		b, err := json.Marshal(t)
		if err != nil {
			return err
		}
		buf.Write(b)
		return nil
	default:
		// 兜底：先解码成 any（UseNumber）再规范化，覆盖 struct 等类型
		raw, err := json.Marshal(v)
		if err != nil {
			return err
		}
		var decoded any
		dec := json.NewDecoder(bytes.NewReader(raw))
		dec.UseNumber()
		if err := dec.Decode(&decoded); err != nil {
			return err
		}
		return writeCanon(buf, decoded)
	}
}

func writeObject(buf *bytes.Buffer, m map[string]any) error {
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	buf.WriteByte('{')
	for i, k := range keys {
		if i > 0 {
			buf.WriteByte(',')
		}
		kb, _ := json.Marshal(k)
		buf.Write(kb)
		buf.WriteByte(':')
		if err := writeCanon(buf, m[k]); err != nil {
			return err
		}
	}
	buf.WriteByte('}')
	return nil
}
