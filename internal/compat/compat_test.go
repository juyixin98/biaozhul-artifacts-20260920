package compat

import (
	"testing"
	"time"

	"contractcheck/internal/schema"
)

func mustParse(t *testing.T, s string) *schema.Schema {
	t.Helper()
	sc, err := schema.ParseBytes([]byte(s))
	if err != nil {
		t.Fatalf("解析 schema 失败: %v\n%s", err, s)
	}
	return sc
}

var fixedNow = time.Date(2026, 9, 25, 10, 0, 0, 0, time.UTC)

// 相同 schema 在两个方向上都必须兼容。
func TestIdenticalSchemasCompatible(t *testing.T) {
	s := mustParse(t, `{
		"type": "object",
		"properties": {"age": {"type": "integer", "minimum": 0, "maximum": 120}},
		"required": ["age"]
	}`)
	for _, dir := range []Direction{DirectionRequest, DirectionResponse} {
		rep := Check(dir, s, s, fixedNow)
		if rep.Status != StatusCompatible {
			t.Fatalf("方向 %s 期望 compatible，得到 %s: %+v", dir, rep.Status, rep.Findings)
		}
	}
}

// 请求方向：新契约新增必填字段 = 收紧，旧客户端请求被拒，必须报不兼容。
func TestRequestDirection_NewRequiredFieldIsIncompatible(t *testing.T) {
	old := mustParse(t, `{
		"type": "object",
		"properties": {
			"name": {"type": "string"},
			"email": {"type": "string"}
		},
		"required": ["name"]
	}`)
	neu := mustParse(t, `{
		"type": "object",
		"properties": {
			"name": {"type": "string"},
			"email": {"type": "string"}
		},
		"required": ["name", "email"]
	}`)
	rep := Check(DirectionRequest, old, neu, fixedNow)
	if rep.Status != StatusIncompatible {
		t.Fatalf("期望 incompatible，得到 %s", rep.Status)
	}
	if len(rep.Findings) != 1 || rep.Findings[0].Kind != "required" {
		t.Fatalf("期望 1 条 required finding，得到 %+v", rep.Findings)
	}
	f := rep.Findings[0]
	if f.Message == "" || f.Example == nil {
		t.Fatalf("finding 必须包含说明与示例值: %+v", f)
	}
	if _, ok := f.Example.(map[string]any); !ok {
		t.Fatalf("required 反例示例应为对象（缺该字段的请求），得到 %T", f.Example)
	}
}

// 请求方向：新契约把必填改为可选 = 放宽，旧请求仍被接受，兼容。
func TestRequestDirection_DroppingRequiredIsCompatible(t *testing.T) {
	old := mustParse(t, `{"type":"object","required":["a"],"properties":{"a":{"type":"string"}}}`)
	neu := mustParse(t, `{"type":"object","required":[],"properties":{"a":{"type":"string"}}}`)
	rep := Check(DirectionRequest, old, neu, fixedNow)
	if rep.Status != StatusCompatible {
		t.Fatalf("期望 compatible，得到 %s: %+v", rep.Status, rep.Findings)
	}
}

// 响应方向：新服务不再保证输出旧消费者必填的字段 = 放宽产出，不兼容。
func TestResponseDirection_DroppingRequiredIsIncompatible(t *testing.T) {
	old := mustParse(t, `{
		"type": "object",
		"required": ["id", "name"],
		"properties": {"id": {"type": "integer"}, "name": {"type": "string"}}
	}`)
	neu := mustParse(t, `{
		"type": "object",
		"required": ["id"],
		"properties": {"id": {"type": "integer"}, "name": {"type": "string"}}
	}`)
	rep := Check(DirectionResponse, old, neu, fixedNow)
	if rep.Status != StatusIncompatible {
		t.Fatalf("期望 incompatible，得到 %s", rep.Status)
	}
	found := false
	for _, f := range rep.Findings {
		if f.Kind == "required" {
			found = true
		}
	}
	if !found {
		t.Fatalf("期望 required finding，得到 %+v", rep.Findings)
	}
}

// 响应方向：新服务收窄枚举（只输出旧枚举子集），兼容。
func TestResponseDirection_NarrowedEnumCompatible(t *testing.T) {
	old := mustParse(t, `{"type":"string","enum":["red","green","blue"]}`)
	neu := mustParse(t, `{"type":"string","enum":["red"]}`)
	rep := Check(DirectionResponse, old, neu, fixedNow)
	if rep.Status != StatusCompatible {
		t.Fatalf("期望 compatible，得到 %s: %+v", rep.Status, rep.Findings)
	}
}

// 请求方向：新契约收窄枚举，旧客户端可能发送已移除的值，不兼容，且给出示例值。
func TestRequestDirection_NarrowedEnumIncompatibleWithExample(t *testing.T) {
	old := mustParse(t, `{"type":"string","enum":["red","green","blue"]}`)
	neu := mustParse(t, `{"type":"string","enum":["red"]}`)
	rep := Check(DirectionRequest, old, neu, fixedNow)
	if rep.Status != StatusIncompatible {
		t.Fatalf("期望 incompatible，得到 %s", rep.Status)
	}
	examples := map[string]bool{}
	for _, f := range rep.Findings {
		if f.Kind != "enum" {
			t.Fatalf("只应出现 enum findings，得到 %s", f.Kind)
		}
		v, _ := f.Example.(string)
		examples[v] = true
	}
	for _, removed := range []string{"green", "blue"} {
		if !examples[removed] {
			t.Fatalf("期望反例示例包含被移除的枚举值 %q，findings=%+v", removed, rep.Findings)
		}
	}
}

// 请求方向：新契约提高 minimum（[0,100] -> [18,100]），旧值 0 不再被接受。
func TestRequestDirection_RaisedMinimumIncompatible(t *testing.T) {
	old := mustParse(t, `{"type":"integer","minimum":0,"maximum":100}`)
	neu := mustParse(t, `{"type":"integer","minimum":18,"maximum":100}`)
	rep := Check(DirectionRequest, old, neu, fixedNow)
	if rep.Status != StatusIncompatible {
		t.Fatalf("期望 incompatible，得到 %s", rep.Status)
	}
	if rep.Findings[0].Kind != "minimum" {
		t.Fatalf("期望 minimum finding，得到 %+v", rep.Findings)
	}
	if n, ok := rep.Findings[0].Example.(float64); !ok || n >= 18 {
		t.Fatalf("反例示例应是 <18 的数字，得到 %v", rep.Findings[0].Example)
	}
}

// 响应方向：新服务缩小数值范围，产出仍在旧消费者范围内，兼容。
func TestResponseDirection_NarrowedRangeCompatible(t *testing.T) {
	old := mustParse(t, `{"type":"integer","minimum":0,"maximum":100}`)
	neu := mustParse(t, `{"type":"integer","minimum":10,"maximum":90}`)
	rep := Check(DirectionResponse, old, neu, fixedNow)
	if rep.Status != StatusCompatible {
		t.Fatalf("期望 compatible，得到 %s: %+v", rep.Status, rep.Findings)
	}
}

// 响应方向：新服务放宽最大值，可能产出旧消费者无法处理的值，不兼容。
func TestResponseDirection_RaisedMaximumIncompatible(t *testing.T) {
	old := mustParse(t, `{"type":"integer","minimum":0,"maximum":100}`)
	neu := mustParse(t, `{"type":"integer","minimum":0,"maximum":1000}`)
	rep := Check(DirectionResponse, old, neu, fixedNow)
	if rep.Status != StatusIncompatible {
		t.Fatalf("期望 incompatible，得到 %s", rep.Status)
	}
	if rep.Findings[0].Kind != "maximum" {
		t.Fatalf("期望 maximum finding，得到 %+v", rep.Findings)
	}
}

// 类型变更不兼容，并报告字段路径与示例值。
func TestTypeMismatchNestedField(t *testing.T) {
	old := mustParse(t, `{
		"type": "object",
		"properties": {"user": {"type": "object", "properties": {"age": {"type": "integer"}}}},
		"required": ["user"]
	}`)
	neu := mustParse(t, `{
		"type": "object",
		"properties": {"user": {"type": "object", "properties": {"age": {"type": "string"}}}},
		"required": ["user"]
	}`)
	rep := Check(DirectionRequest, old, neu, fixedNow)
	if rep.Status != StatusIncompatible {
		t.Fatalf("期望 incompatible，得到 %s", rep.Status)
	}
	found := false
	for _, f := range rep.Findings {
		if f.Path == "/properties/user/properties/age" && f.Kind == "type" {
			found = true
		}
	}
	if !found {
		t.Fatalf("期望带嵌套路径的 type finding，得到 %+v", rep.Findings)
	}
}

// integer 生产者可被 number 消费者接受（子类型）。
func TestIntegerCoveredByNumber(t *testing.T) {
	old := mustParse(t, `{"type":"integer"}`)
	neu := mustParse(t, `{"type":"number"}`)
	if rep := Check(DirectionRequest, old, neu, fixedNow); rep.Status != StatusCompatible {
		t.Fatalf("integer->number 请求方向应兼容，得到 %s: %+v", rep.Status, rep.Findings)
	}
	// 反向：number 生产者产出 1.5，integer 消费者拒绝。
	rep := Check(DirectionRequest, neu, old, fixedNow)
	if rep.Status != StatusIncompatible {
		t.Fatalf("number->integer 请求方向应不兼容，得到 %s", rep.Status)
	}
}

// 不支持的关键字：任何一方出现都必须判 unknown，绝不判通过。
func TestUnsupportedKeywordReturnsUnknown(t *testing.T) {
	old := mustParse(t, `{"type":"string"}`)
	neu := mustParse(t, `{"type":"string","pattern":"^[a-z]+$"}`)
	rep := Check(DirectionRequest, old, neu, fixedNow)
	if rep.Status != StatusUnknown {
		t.Fatalf("期望 unknown，得到 %s", rep.Status)
	}
	if len(rep.UnknownKeywords) != 1 {
		t.Fatalf("期望 1 个未知关键字，得到 %+v", rep.UnknownKeywords)
	}
	u := rep.UnknownKeywords[0]
	if u.Keyword != "pattern" || u.Side != "new" || u.Path != "" {
		t.Fatalf("未知关键字定位错误: %+v", u)
	}
	if len(rep.Findings) != 0 {
		t.Fatalf("unknown 时不应输出兼容结论 findings，得到 %+v", rep.Findings)
	}
}

// 嵌套在子 schema 中的不支持关键字也要被发现并给出定位。
func TestUnsupportedKeywordNested(t *testing.T) {
	old := mustParse(t, `{
		"type": "object",
		"properties": {"addr": {"type": "object", "properties": {"zip": {"type":"string","format":"zip-code"}}}}
	}`)
	neu := mustParse(t, `{"type":"object"}`)
	rep := Check(DirectionResponse, old, neu, fixedNow)
	if rep.Status != StatusUnknown {
		t.Fatalf("期望 unknown，得到 %s", rep.Status)
	}
	u := rep.UnknownKeywords[0]
	if u.Keyword != "format" || u.Path != "/properties/addr/properties/zip" {
		t.Fatalf("期望定位到嵌套 format，得到 %+v", u)
	}
}

// 多种不兼容同时存在（枚举 + 数值 + 必填）时全部报告。
func TestMultipleFindingsReported(t *testing.T) {
	old := mustParse(t, `{
		"type": "object",
		"properties": {
			"role": {"type": "string", "enum": ["admin", "user"]},
			"age":  {"type": "integer", "minimum": 0}
		},
		"required": ["role"]
	}`)
	neu := mustParse(t, `{
		"type": "object",
		"properties": {
			"role": {"type": "string", "enum": ["admin"]},
			"age":  {"type": "integer", "minimum": 18}
		},
		"required": ["role", "age"]
	}`)
	rep := Check(DirectionRequest, old, neu, fixedNow)
	if rep.Status != StatusIncompatible {
		t.Fatalf("期望 incompatible，得到 %s", rep.Status)
	}
	kinds := map[string]int{}
	for _, f := range rep.Findings {
		kinds[f.Kind]++
	}
	for _, k := range []string{"enum", "minimum", "required"} {
		if kinds[k] == 0 {
			t.Fatalf("期望同时报告 %s，实际 findings=%+v", k, rep.Findings)
		}
	}
}

// checkedAt 使用注入的时钟时间（UTC）。
func TestCheckedAtUsesProvidedClock(t *testing.T) {
	s := mustParse(t, `{"type":"string"}`)
	rep := Check(DirectionRequest, s, s, fixedNow)
	if !rep.CheckedAt.Equal(fixedNow) {
		t.Fatalf("期望 %v，得到 %v", fixedNow, rep.CheckedAt)
	}
}
