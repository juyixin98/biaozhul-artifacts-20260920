package schema

import "testing"

// 补充解析错误与边界用例，覆盖各关键字的分支。

func TestParseErrors(t *testing.T) {
	cases := map[string]string{
		"空输入":            ``,
		"非标量 schema":     `123`,
		"type 非字符串":      `{"type": 1}`,
		"type 空数组":       `{"type": []}`,
		"enum 非数组":       `{"enum": "x"}`,
		"enum 空数组":       `{"enum": []}`,
		"required 非数组":   `{"required": "a"}`,
		"required 空串":    `{"required": [""]}`,
		"properties 非对象": `{"properties": []}`,
		"minimum 非数字":    `{"minimum": "0"}`,
		"minLength 非整数":  `{"minLength": 1.5}`,
		"items 非法":       `{"items": 5}`,
		"嵌套非法":           `{"properties": {"a": {"type": "bad"}}}`,
	}
	for name, input := range cases {
		t.Run(name, func(t *testing.T) {
			if _, err := ParseBytes([]byte(input)); err == nil {
				t.Fatalf("输入 %s 应报错", input)
			}
		})
	}
}

func TestParseStringConstraints(t *testing.T) {
	s, err := ParseBytes([]byte(`{"type":"string","minLength":2,"maxLength":5}`))
	if err != nil {
		t.Fatal(err)
	}
	if s.MinLength == nil || *s.MinLength != 2 || s.MaxLength == nil || *s.MaxLength != 5 {
		t.Fatalf("字符串长度约束解析错误: %+v", s)
	}
}

func TestParseItemsAndNestedUnknown(t *testing.T) {
	s, err := ParseBytes([]byte(`{
		"type": "array",
		"items": {"type": "object", "properties": {"x": {"type": "integer", "multipleOf": 2}}}
	}`))
	if err != nil {
		t.Fatal(err)
	}
	if s.Items == nil || s.Items.Properties["x"] == nil {
		t.Fatal("items 嵌套解析失败")
	}
	if len(s.Unknown) != 1 || s.Unknown[0].Keyword != "multipleOf" {
		t.Fatalf("嵌套未知关键字应上浮到根: %+v", s.Unknown)
	}
	if s.Unknown[0].Path != "/items/properties/x" {
		t.Fatalf("未知关键字路径错误: %+v", s.Unknown[0])
	}
}

func TestParseAdditionalPropertiesSchema(t *testing.T) {
	s, err := ParseBytes([]byte(`{"type":"object","additionalProperties":{"type":"string"}}`))
	if err != nil {
		t.Fatal(err)
	}
	if !s.HasAdditionalProps || s.AdditionalProps.Type[0] != "string" {
		t.Fatalf("additionalProperties schema 解析错误: %+v", s.AdditionalProps)
	}
}

func TestParseNullType(t *testing.T) {
	s, err := ParseBytes([]byte(`{"type":"null"}`))
	if err != nil {
		t.Fatal(err)
	}
	if s.Type[0] != TypeNull {
		t.Fatalf("null 类型解析错误: %v", s.Type)
	}
}

func TestParseEnumMixedTypes(t *testing.T) {
	s, err := ParseBytes([]byte(`{"enum":[1,"two",true,null,{"k":"v"},[1,2]]}`))
	if err != nil {
		t.Fatal(err)
	}
	if len(s.Enum) != 6 {
		t.Fatalf("混合枚举解析错误: %+v", s.Enum)
	}
}
