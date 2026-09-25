package compat

import (
	"testing"
)

// 字符串长度：请求方向收紧 minLength 不兼容。
func TestMinLengthTightened(t *testing.T) {
	old := mustParse(t, `{"type":"string","minLength":1}`)
	neu := mustParse(t, `{"type":"string","minLength":5}`)
	rep := Check(DirectionRequest, old, neu, fixedNow)
	if rep.Status != StatusIncompatible || rep.Findings[0].Kind != "minLength" {
		t.Fatalf("期望 minLength 不兼容: %+v", rep)
	}
	if s, ok := rep.Findings[0].Example.(string); !ok || len(s) >= 5 {
		t.Fatalf("反例应是长度<5 的字符串: %v", rep.Findings[0].Example)
	}
}

// 字符串长度：响应方向新服务可能产出超长字符串，不兼容。
func TestMaxLengthWidenedResponse(t *testing.T) {
	old := mustParse(t, `{"type":"string","maxLength":10}`)
	neu := mustParse(t, `{"type":"string","maxLength":100}`)
	rep := Check(DirectionResponse, old, neu, fixedNow)
	if rep.Status != StatusIncompatible || rep.Findings[0].Kind != "maxLength" {
		t.Fatalf("期望 maxLength 不兼容: %+v", rep)
	}
	if s, ok := rep.Findings[0].Example.(string); !ok || len(s) != 11 {
		t.Fatalf("反例应是长度 11 的字符串: %q", rep.Findings[0].Example)
	}
}

// 数组元素：新契约把元素类型从 string 改为 integer，请求方向不兼容。
func TestArrayItemsTypeChange(t *testing.T) {
	old := mustParse(t, `{"type":"array","items":{"type":"string"}}`)
	neu := mustParse(t, `{"type":"array","items":{"type":"integer"}}`)
	rep := Check(DirectionRequest, old, neu, fixedNow)
	if rep.Status != StatusIncompatible {
		t.Fatalf("期望 incompatible: %+v", rep)
	}
	found := false
	for _, f := range rep.Findings {
		if f.Path == "/items" && f.Kind == "type" {
			found = true
		}
	}
	if !found {
		t.Fatalf("期望 /items 路径的 type finding: %+v", rep.Findings)
	}
}

// 消费者约束了 items 而生产者未约束：不兼容。
func TestArrayItemsUnconstrainedProducer(t *testing.T) {
	old := mustParse(t, `{"type":"array"}`)
	neu := mustParse(t, `{"type":"array","items":{"type":"integer"}}`)
	rep := Check(DirectionRequest, old, neu, fixedNow)
	if rep.Status != StatusIncompatible || rep.Findings[0].Kind != "items" {
		t.Fatalf("期望 items 不兼容: %+v", rep)
	}
}

// additionalProperties:false 的消费者遇到开放生产者：不兼容。
func TestAdditionalPropertiesClosedConsumer(t *testing.T) {
	old := mustParse(t, `{"type":"object","properties":{"a":{"type":"string"}}}`)
	neu := mustParse(t, `{"type":"object","properties":{"a":{"type":"string"}},"additionalProperties":false}`)
	rep := Check(DirectionRequest, old, neu, fixedNow)
	if rep.Status != StatusIncompatible {
		t.Fatalf("期望 incompatible: %+v", rep)
	}
	found := false
	for _, f := range rep.Findings {
		if f.Kind == "additionalProperties" {
			found = true
		}
	}
	if !found {
		t.Fatalf("期望 additionalProperties finding: %+v", rep.Findings)
	}
}

// 生产者声明了消费者禁止的具名字段：不兼容，且示例值包含该字段。
func TestNamedPropertyForbiddenByConsumer(t *testing.T) {
	old := mustParse(t, `{"type":"object","properties":{"extra":{"type":"string"}}}`)
	neu := mustParse(t, `{"type":"object","additionalProperties":false}`)
	rep := Check(DirectionRequest, old, neu, fixedNow)
	if rep.Status != StatusIncompatible {
		t.Fatalf("期望 incompatible: %+v", rep)
	}
	f := rep.Findings[0]
	if f.Kind != "additionalProperties" || f.Path != "/properties/extra" {
		t.Fatalf("finding 定位错误: %+v", f)
	}
	if m, ok := f.Example.(map[string]any); !ok || m["extra"] == nil {
		t.Fatalf("示例应包含 extra 字段: %v", f.Example)
	}
}

// 双方都不限额外字段：兼容。
func TestBothOpenObjectsCompatible(t *testing.T) {
	old := mustParse(t, `{"type":"object","properties":{"a":{"type":"string"}}}`)
	neu := mustParse(t, `{"type":"object","properties":{"a":{"type":"string"},"b":{"type":"integer"}}}`)
	rep := Check(DirectionRequest, old, neu, fixedNow)
	if rep.Status != StatusCompatible {
		t.Fatalf("期望 compatible: %+v", rep.Findings)
	}
}

// 布尔 schema：false 生产者是任何消费者的子集。
func TestBoolFalseProducerAlwaysCompatible(t *testing.T) {
	p := mustParse(t, `false`)
	c := mustParse(t, `{"type":"string","minLength":3}`)
	rep := Check(DirectionRequest, p, c, fixedNow)
	if rep.Status != StatusCompatible {
		t.Fatalf("false 生产者应兼容一切: %+v", rep.Findings)
	}
}

// 布尔 schema：true 生产者 vs 有约束的消费者，不兼容。
func TestBoolTrueProducerVsConstrainedConsumer(t *testing.T) {
	p := mustParse(t, `true`)
	c := mustParse(t, `{"type":"string"}`)
	rep := Check(DirectionRequest, p, c, fixedNow)
	if rep.Status != StatusIncompatible {
		t.Fatalf("true 生产者对受限消费者应不兼容: %+v", rep)
	}
}

// 布尔 schema：false 消费者拒绝一切非空生产者。
func TestBoolFalseConsumer(t *testing.T) {
	p := mustParse(t, `{"type":"string"}`)
	c := mustParse(t, `false`)
	rep := Check(DirectionRequest, p, c, fixedNow)
	if rep.Status != StatusIncompatible {
		t.Fatalf("false 消费者应拒绝: %+v", rep)
	}
}

// true 消费者接受一切。
func TestBoolTrueConsumer(t *testing.T) {
	p := mustParse(t, `{"type":"string","minLength":1}`)
	c := mustParse(t, `true`)
	rep := Check(DirectionRequest, p, c, fixedNow)
	if rep.Status != StatusCompatible {
		t.Fatalf("true 消费者应接受一切: %+v", rep.Findings)
	}
}

// 无类型生产者 vs 有类型消费者：不兼容并给出反例类型。
func TestUntypedProducerVsTypedConsumer(t *testing.T) {
	p := mustParse(t, `{"minimum": 0}`)
	c := mustParse(t, `{"type":"integer"}`)
	rep := Check(DirectionRequest, p, c, fixedNow)
	if rep.Status != StatusIncompatible {
		t.Fatalf("期望 incompatible: %+v", rep)
	}
}

// 生产者无枚举、消费者限定枚举：不兼容，反例在枚举之外。
func TestUnenumProducerVsEnumConsumer(t *testing.T) {
	p := mustParse(t, `{"type":"string"}`)
	c := mustParse(t, `{"type":"string","enum":["a","b"]}`)
	rep := Check(DirectionRequest, p, c, fixedNow)
	if rep.Status != StatusIncompatible || rep.Findings[0].Kind != "enum" {
		t.Fatalf("期望 enum 不兼容: %+v", rep)
	}
	ex := rep.Findings[0].Example.(string)
	if ex == "a" || ex == "b" {
		t.Fatalf("反例不应在消费者枚举中: %q", ex)
	}
}

// 数值型生产者 vs 数值枚举消费者：反例应为数字。
func TestNumericProducerVsEnumConsumer(t *testing.T) {
	p := mustParse(t, `{"type":"integer"}`)
	c := mustParse(t, `{"enum":[1,2,3]}`)
	rep := Check(DirectionRequest, p, c, fixedNow)
	if rep.Status != StatusIncompatible {
		t.Fatalf("期望 incompatible: %+v", rep)
	}
	if _, ok := rep.Findings[0].Example.(float64); !ok {
		t.Fatalf("数值反例应为数字: %v", rep.Findings[0].Example)
	}
}

// 枚举值对象/数组的深比较。
func TestEnumDeepEquality(t *testing.T) {
	old := mustParse(t, `{"enum":[[1,2],[3]]}`)
	neu := mustParse(t, `{"enum":[[1,2]]}`)
	rep := Check(DirectionRequest, old, neu, fixedNow)
	if rep.Status != StatusIncompatible {
		t.Fatalf("期望 incompatible: %+v", rep)
	}
	// 相同枚举则兼容。
	same := mustParse(t, `{"enum":[[1,2],[3]]}`)
	if rep2 := Check(DirectionRequest, old, same, fixedNow); rep2.Status != StatusCompatible {
		t.Fatalf("相同枚举应兼容: %+v", rep2.Findings)
	}
}

// 未知方向返回 unknown。
func TestUnknownDirection(t *testing.T) {
	s := mustParse(t, `{"type":"string"}`)
	rep := Check(Direction("sideways"), s, s, fixedNow)
	if rep.Status != StatusUnknown {
		t.Fatalf("未知方向应 unknown: %+v", rep)
	}
}

// 双方都有 minimum 且生产者下限更高：兼容（生产者范围更窄）。
func TestProducerNarrowerMinimumCompatible(t *testing.T) {
	old := mustParse(t, `{"type":"integer","minimum":10}`)
	neu := mustParse(t, `{"type":"integer","minimum":0}`)
	rep := Check(DirectionRequest, old, neu, fixedNow)
	if rep.Status != StatusCompatible {
		t.Fatalf("期望 compatible: %+v", rep.Findings)
	}
}

// additionalProperties 为 schema 时的递归检查。
func TestAdditionalPropertiesSchemaRecursion(t *testing.T) {
	old := mustParse(t, `{"type":"object","properties":{"x":{"type":"string"}}}`)
	neu := mustParse(t, `{"type":"object","additionalProperties":{"type":"integer"}}`)
	rep := Check(DirectionRequest, old, neu, fixedNow)
	if rep.Status != StatusIncompatible {
		t.Fatalf("string 字段对 integer additionalProperties 应不兼容: %+v", rep)
	}
}

// 双方都有 additionalProperties schema 的递归。
func TestBothAdditionalPropertiesSchemas(t *testing.T) {
	old := mustParse(t, `{"type":"object","additionalProperties":{"type":"string"}}`)
	neu := mustParse(t, `{"type":"object","additionalProperties":{"type":"integer"}}`)
	rep := Check(DirectionRequest, old, neu, fixedNow)
	if rep.Status != StatusIncompatible {
		t.Fatalf("期望 incompatible: %+v", rep)
	}
}

// minLength=0 的消费者边界。
func TestMinLengthZeroBoundary(t *testing.T) {
	old := mustParse(t, `{"type":"string"}`)
	neu := mustParse(t, `{"type":"string","minLength":1}`)
	rep := Check(DirectionRequest, old, neu, fixedNow)
	if rep.Status != StatusIncompatible {
		t.Fatalf("期望 incompatible: %+v", rep)
	}
	if s := rep.Findings[0].Example.(string); s != "" {
		t.Fatalf("反例应为空串: %q", s)
	}
}

// 消费者有 maximum 而生产者无上限：反例取消费者上限+1。
func TestMaximumNoProducerBound(t *testing.T) {
	old := mustParse(t, `{"type":"integer"}`)
	neu := mustParse(t, `{"type":"integer","maximum":50}`)
	rep := Check(DirectionRequest, old, neu, fixedNow)
	if rep.Status != StatusIncompatible {
		t.Fatalf("期望 incompatible: %+v", rep)
	}
	if n := rep.Findings[0].Example.(float64); n != 51 {
		t.Fatalf("反例应为 51: %v", rep.Findings[0].Example)
	}
}

// 生产者枚举值违反消费者数值约束时仍按枚举成员判定（enum 路径优先）。
func TestProducerEnumCheckedAgainstConsumerEnum(t *testing.T) {
	old := mustParse(t, `{"enum":["x","y"]}`)
	neu := mustParse(t, `{"enum":["x","y","z"]}`)
	rep := Check(DirectionRequest, old, neu, fixedNow)
	if rep.Status != StatusCompatible {
		t.Fatalf("子集枚举应兼容: %+v", rep.Findings)
	}
}

// 响应方向：新服务新增必填字段（保证更多），兼容。
func TestResponseDirection_AddedRequiredCompatible(t *testing.T) {
	old := mustParse(t, `{"type":"object","required":["id"],"properties":{"id":{"type":"integer"},"name":{"type":"string"}}}`)
	neu := mustParse(t, `{"type":"object","required":["id","name"],"properties":{"id":{"type":"integer"},"name":{"type":"string"}}}`)
	rep := Check(DirectionResponse, old, neu, fixedNow)
	if rep.Status != StatusCompatible {
		t.Fatalf("期望 compatible: %+v", rep.Findings)
	}
}

// items 双方都无约束：兼容。
func TestArrayBothUnconstrained(t *testing.T) {
	old := mustParse(t, `{"type":"array"}`)
	neu := mustParse(t, `{"type":"array"}`)
	rep := Check(DirectionRequest, old, neu, fixedNow)
	if rep.Status != StatusCompatible {
		t.Fatalf("期望 compatible: %+v", rep.Findings)
	}
}

// 类型数组：生产者 ["string","null"] vs 消费者 ["string"] 不兼容。
func TestTypeArrays(t *testing.T) {
	old := mustParse(t, `{"type":["string","null"]}`)
	neu := mustParse(t, `{"type":["string"]}`)
	rep := Check(DirectionRequest, old, neu, fixedNow)
	if rep.Status != StatusIncompatible {
		t.Fatalf("期望 incompatible: %+v", rep)
	}
	if rep.Findings[0].Example != nil {
		t.Fatalf("null 类型的反例示例应为 null: %v", rep.Findings[0].Example)
	}
}
