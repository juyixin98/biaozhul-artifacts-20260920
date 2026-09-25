package schema

import "testing"

func TestParseBasicConstraints(t *testing.T) {
	s, err := ParseBytes([]byte(`{
		"type": "integer",
		"minimum": 0,
		"maximum": 100,
		"enum": [1, 2, 3]
	}`))
	if err != nil {
		t.Fatal(err)
	}
	if !s.HasType || s.Type[0] != TypeInteger {
		t.Fatalf("type 解析错误: %+v", s.Type)
	}
	if s.Minimum == nil || *s.Minimum != 0 || s.Maximum == nil || *s.Maximum != 100 {
		t.Fatalf("数值范围解析错误: %+v %+v", s.Minimum, s.Maximum)
	}
	if !s.HasEnum || len(s.Enum) != 3 {
		t.Fatalf("enum 解析错误: %+v", s.Enum)
	}
}

func TestParseTypeArray(t *testing.T) {
	s, err := ParseBytes([]byte(`{"type": ["string", "null"]}`))
	if err != nil {
		t.Fatal(err)
	}
	if len(s.Type) != 2 {
		t.Fatalf("期望 2 个类型，得到 %v", s.Type)
	}
}

func TestParseObjectAndRequired(t *testing.T) {
	s, err := ParseBytes([]byte(`{
		"type": "object",
		"properties": {"a": {"type": "string"}},
		"required": ["a"]
	}`))
	if err != nil {
		t.Fatal(err)
	}
	if len(s.Required) != 1 || s.Required[0] != "a" {
		t.Fatalf("required 错误: %v", s.Required)
	}
	if s.Properties["a"] == nil || s.Properties["a"].Type[0] != "string" {
		t.Fatalf("嵌套属性解析错误: %+v", s.Properties)
	}
}

func TestBooleanSchemas(t *testing.T) {
	t.Run("true", func(t *testing.T) {
		s, err := ParseBytes([]byte(`true`))
		if err != nil {
			t.Fatal(err)
		}
		if s.Bool == nil || !*s.Bool {
			t.Fatalf("期望布尔 true")
		}
	})
	t.Run("false", func(t *testing.T) {
		s, err := ParseBytes([]byte(`false`))
		if err != nil {
			t.Fatal(err)
		}
		if s.Bool == nil || *s.Bool {
			t.Fatalf("期望布尔 false")
		}
	})
}

func TestAnnotationsIgnored(t *testing.T) {
	s, err := ParseBytes([]byte(`{
		"$schema": "https://json-schema.org/draft/2020-12/schema",
		"title": "T", "description": "D", "default": 1, "examples": [1],
		"type": "integer"
	}`))
	if err != nil {
		t.Fatal(err)
	}
	if len(s.Unknown) != 0 {
		t.Fatalf("注解关键字不应算未知: %+v", s.Unknown)
	}
}

func TestUnknownKeywordsRecorded(t *testing.T) {
	s, err := ParseBytes([]byte(`{"type":"string","oneOf":[],"const":"x"}`))
	if err != nil {
		t.Fatal(err)
	}
	got := map[string]bool{}
	for _, u := range s.Unknown {
		got[u.Keyword] = true
	}
	if !got["oneOf"] || !got["const"] {
		t.Fatalf("期望记录 oneOf/const，得到 %+v", s.Unknown)
	}
}

func TestUnknownKeywordPath(t *testing.T) {
	s, err := ParseBytes([]byte(`{
		"type": "object",
		"properties": {"a": {"type": "array", "items": {"type": "string", "pattern": "x"}}}
	}`))
	if err != nil {
		t.Fatal(err)
	}
	if len(s.Unknown) != 1 {
		t.Fatalf("期望 1 个未知关键字，得到 %+v", s.Unknown)
	}
	if s.Unknown[0].Path != "/properties/a/items" || s.Unknown[0].Keyword != "pattern" {
		t.Fatalf("路径定位错误: %+v", s.Unknown[0])
	}
}

func TestInvalidJSON(t *testing.T) {
	if _, err := ParseBytes([]byte(`{not json`)); err == nil {
		t.Fatal("非法 JSON 应报错")
	}
}

func TestInvalidType(t *testing.T) {
	if _, err := ParseBytes([]byte(`{"type":"datetime"}`)); err == nil {
		t.Fatal("不支持的类型应报错")
	}
}

func TestNegativeMinLengthRejected(t *testing.T) {
	if _, err := ParseBytes([]byte(`{"type":"string","minLength":-1}`)); err == nil {
		t.Fatal("负 minLength 应报错")
	}
}

func TestDuplicateRequiredRejected(t *testing.T) {
	if _, err := ParseBytes([]byte(`{"type":"object","required":["a","a"]}`)); err == nil {
		t.Fatal("重复 required 应报错")
	}
}

func TestAdditionalPropertiesFalse(t *testing.T) {
	s, err := ParseBytes([]byte(`{"type":"object","additionalProperties":false}`))
	if err != nil {
		t.Fatal(err)
	}
	if !s.HasAdditionalProps || s.AdditionalProps.Bool == nil || *s.AdditionalProps.Bool {
		t.Fatalf("additionalProperties:false 解析错误")
	}
}
