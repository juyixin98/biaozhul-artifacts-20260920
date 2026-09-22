package util

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"sort"
)

// ContentHash 计算事件内容的稳定哈希，用于检测“同 event_id 但内容不同”。
// 元数据按 key 排序后序列化，保证 JSON 键顺序不影响哈希；
// 时间戳统一为 UTC 纳秒，字段缺失与空值语义一致。
func ContentHash(eventType, employeeID string, occurredUTCUnixNano int64, metadata []byte) string {
	canonical := canonicalMetadata(metadata)
	h := sha256.New()
	h.Write([]byte(eventType))
	h.Write([]byte{0})
	h.Write([]byte(employeeID))
	h.Write([]byte{0})
	h.Write(itob(occurredUTCUnixNano))
	h.Write([]byte{0})
	h.Write(canonical)
	return hex.EncodeToString(h.Sum(nil))
}

func itob(v int64) []byte {
	// 十进制字节即可，稳定且可读
	return []byte(itoa(v))
}

func itoa(v int64) string {
	if v == 0 {
		return "0"
	}
	neg := v < 0
	if neg {
		v = -v
	}
	var b [20]byte
	i := len(b)
	for v > 0 {
		i--
		b[i] = byte('0' + v%10)
		v /= 10
	}
	if neg {
		i--
		b[i] = '-'
	}
	return string(b[i:])
}

// canonicalMetadata 把任意 JSON 对象规范成键有序的紧凑 JSON；
// 非法/空元数据一律归一化为空对象，保证哈希确定性。
func canonicalMetadata(raw []byte) []byte {
	if len(raw) == 0 {
		return []byte("{}")
	}
	var m map[string]any
	if err := json.Unmarshal(raw, &m); err != nil || m == nil {
		return []byte("{}")
	}
	return marshalSorted(m)
}

func marshalSorted(m map[string]any) []byte {
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	sort.Strings(keys)

	var b []byte
	b = append(b, '{')
	for i, k := range keys {
		if i > 0 {
			b = append(b, ',')
		}
		kb, _ := json.Marshal(k)
		b = append(b, kb...)
		b = append(b, ':')
		b = append(b, canonicalValue(m[k])...)
	}
	b = append(b, '}')
	return b
}

func canonicalValue(v any) []byte {
	switch t := v.(type) {
	case map[string]any:
		return marshalSorted(t)
	case []any:
		var b []byte
		b = append(b, '[')
		for i, e := range t {
			if i > 0 {
				b = append(b, ',')
			}
			b = append(b, canonicalValue(e)...)
		}
		b = append(b, ']')
		return b
	default:
		b, _ := json.Marshal(t)
		return b
	}
}
