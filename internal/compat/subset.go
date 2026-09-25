package compat

import (
	"fmt"
	"sort"
	"strings"

	"contractcheck/internal/schema"
)

// checkSubset 校验“生产者合法值集 ⊆ 消费者接受值集”，
// 把每一处反例（生产者允许而消费者拒绝）追加到 findings。
// pointer 是当前节点的 JSON Pointer 路径。
func checkSubset(producer, consumer *schema.Schema, pointer string, findings *[]Finding) {
	// 布尔模式的短路处理。
	if producer.Bool != nil {
		if !*producer.Bool {
			return // 生产者拒绝一切，空集是任何集合的子集。
		}
		// 生产者接受一切：消费者必须也接受一切。
		if !acceptsEverything(consumer) {
			add(findings, pointer, "type",
				"生产者接受任意值，但消费者存在约束",
				exampleForType(firstUncoveredType(producer, consumer)))
		}
		return
	}
	if consumer.Bool != nil {
		if *consumer.Bool {
			return // 消费者接受一切，子集关系恒成立。
		}
		add(findings, pointer, "type", "消费者拒绝一切值（false schema）", exampleValue(producer))
		return
	}

	checkTypes(producer, consumer, pointer, findings)
	checkEnum(producer, consumer, pointer, findings)
	checkNumeric(producer, consumer, pointer, findings)
	checkString(producer, consumer, pointer, findings)
	checkObject(producer, consumer, pointer, findings)
	checkArray(producer, consumer, pointer, findings)
}

// acceptsEverything 判断 schema 是否没有任何约束（忽略未知关键字，
// 因为未知关键字已在 Check 入口拦截为 unknown）。
func acceptsEverything(s *schema.Schema) bool {
	if s.Bool != nil {
		return *s.Bool
	}
	return !s.HasType && !s.HasEnum &&
		s.Minimum == nil && s.Maximum == nil &&
		s.MinLength == nil && s.MaxLength == nil &&
		len(s.Properties) == 0 && len(s.Required) == 0 &&
		s.Items == nil && !s.HasAdditionalProps
}

// typeCovered 判断生产者类型 pt 是否被消费者类型集合接受。
// integer 是 number 的子类型，故 integer 可被 number 覆盖。
func typeCovered(pt string, consumer *schema.Schema) bool {
	if !consumer.HasType {
		return true
	}
	for _, ct := range consumer.Type {
		if ct == pt || (pt == schema.TypeInteger && ct == schema.TypeNumber) {
			return true
		}
	}
	return false
}

func firstUncoveredType(producer, consumer *schema.Schema) string {
	types := producer.Type
	if !producer.HasType {
		types = []string{schema.TypeString, schema.TypeNumber, schema.TypeBoolean, schema.TypeObject, schema.TypeArray, schema.TypeNull}
	}
	for _, pt := range types {
		if !typeCovered(pt, consumer) {
			return pt
		}
	}
	return schema.TypeNull
}

func checkTypes(producer, consumer *schema.Schema, pointer string, findings *[]Finding) {
	if !consumer.HasType {
		return
	}
	types := producer.Type
	if !producer.HasType {
		// 生产者未声明类型：可能产出任意类型，取一个不被消费者接受的做反例。
		types = []string{schema.TypeString, schema.TypeNumber, schema.TypeBoolean, schema.TypeObject, schema.TypeArray, schema.TypeNull}
	}
	for _, pt := range types {
		if !typeCovered(pt, consumer) {
			add(findings, pointer, "type",
				fmt.Sprintf("生产者类型 %q 不被消费者类型 %v 接受", pt, consumer.Type),
				exampleForType(pt))
		}
	}
}

func checkEnum(producer, consumer *schema.Schema, pointer string, findings *[]Finding) {
	if !consumer.HasEnum {
		return
	}
	if producer.HasEnum {
		for _, v := range producer.Enum {
			if !enumContains(consumer.Enum, v) {
				add(findings, pointer, "enum",
					fmt.Sprintf("生产者枚举值 %v 不在消费者枚举 %v 中", v, consumer.Enum),
					v)
			}
		}
		return
	}
	// 生产者未限定枚举：构造一个生产者合法但不在消费者枚举中的值。
	v := valueOutsideEnum(producer, consumer.Enum)
	add(findings, pointer, "enum",
		fmt.Sprintf("生产者未限定枚举，可能产生消费者枚举 %v 之外的值", consumer.Enum),
		v)
}

// enumContains 用规范化后的深比较判断枚举成员。
func enumContains(enum []any, v any) bool {
	for _, e := range enum {
		if deepEqual(e, v) {
			return true
		}
	}
	return false
}

func deepEqual(a, b any) bool {
	switch av := a.(type) {
	case map[string]any:
		bv, ok := b.(map[string]any)
		if !ok || len(av) != len(bv) {
			return false
		}
		for k, sub := range av {
			if !deepEqual(sub, bv[k]) {
				return false
			}
		}
		return true
	case []any:
		bv, ok := b.([]any)
		if !ok || len(av) != len(bv) {
			return false
		}
		for i := range av {
			if !deepEqual(av[i], bv[i]) {
				return false
			}
		}
		return true
	default:
		return a == b
	}
}

func checkNumeric(producer, consumer *schema.Schema, pointer string, findings *[]Finding) {
	if consumer.Minimum != nil {
		if producer.Minimum == nil || *producer.Minimum < *consumer.Minimum {
			lo := *consumer.Minimum - 1
			if producer.Minimum != nil && *producer.Minimum > lo {
				lo = *producer.Minimum
			}
			add(findings, pointer, "minimum",
				fmt.Sprintf("生产者允许小于消费者下限 %v 的值", *consumer.Minimum),
				lo)
		}
	}
	if consumer.Maximum != nil {
		if producer.Maximum == nil || *producer.Maximum > *consumer.Maximum {
			hi := *consumer.Maximum + 1
			if producer.Maximum != nil && *producer.Maximum < hi {
				hi = *producer.Maximum
			}
			add(findings, pointer, "maximum",
				fmt.Sprintf("生产者允许大于消费者上限 %v 的值", *consumer.Maximum),
				hi)
		}
	}
}

func checkString(producer, consumer *schema.Schema, pointer string, findings *[]Finding) {
	if consumer.MinLength != nil {
		if producer.MinLength == nil || *producer.MinLength < *consumer.MinLength {
			n := *consumer.MinLength - 1
			if n < 0 {
				n = 0
			}
			add(findings, pointer, "minLength",
				fmt.Sprintf("生产者允许长度小于消费者下限 %d 的字符串", *consumer.MinLength),
				strings.Repeat("x", n))
		}
	}
	if consumer.MaxLength != nil {
		if producer.MaxLength == nil || *producer.MaxLength > *consumer.MaxLength {
			add(findings, pointer, "maxLength",
				fmt.Sprintf("生产者允许长度大于消费者上限 %d 的字符串", *consumer.MaxLength),
				strings.Repeat("x", *consumer.MaxLength+1))
		}
	}
}

func checkObject(producer, consumer *schema.Schema, pointer string, findings *[]Finding) {
	// 消费者要求的每个必填字段，生产者都必须保证提供。
	for _, req := range consumer.Required {
		if !containsString(producer.Required, req) {
			add(findings, pointer, "required",
				fmt.Sprintf("消费者要求必填字段 %q，但生产者不保证提供", req),
				map[string]any{})
		}
	}

	// 生产者可能产出的每个字段，消费者都必须能接受。
	for _, name := range sortedKeys(producer.Properties) {
		sub := producer.Properties[name]
		childPointer := pointer + "/properties/" + name
		if csub, ok := consumer.Properties[name]; ok {
			checkSubset(sub, csub, childPointer, findings)
			continue
		}
		// 消费者未声明该字段：看 additionalProperties。
		switch {
		case !consumer.HasAdditionalProps:
			// JSON Schema 默认允许额外字段，无冲突。
		case consumer.AdditionalProps != nil && consumer.AdditionalProps.Bool != nil && !*consumer.AdditionalProps.Bool:
			add(findings, childPointer, "additionalProperties",
				fmt.Sprintf("生产者可能输出字段 %q，但消费者禁止额外字段", name),
				map[string]any{name: exampleValue(sub)})
		case consumer.AdditionalProps != nil:
			checkSubset(sub, consumer.AdditionalProps, childPointer, findings)
		}
	}

	// 生产者的额外字段（additionalProperties）必须被消费者接受。
	checkAdditionalProperties(producer, consumer, pointer, findings)
}

// checkAdditionalProperties 比较双方的 additionalProperties 约束。
// 未声明 additionalProperties 按 JSON Schema 默认视为 true（接受任意额外字段）。
func checkAdditionalProperties(producer, consumer *schema.Schema, pointer string, findings *[]Finding) {
	apPointer := pointer + "/additionalProperties"

	// 生产者禁止额外字段：不会产出额外字段，恒兼容。
	if producer.HasAdditionalProps && isBoolFalse(producer.AdditionalProps) {
		return
	}
	// 消费者未声明：默认接受一切额外字段，恒兼容。
	if !consumer.HasAdditionalProps {
		return
	}
	// 消费者接受一切额外字段：恒兼容。
	if acceptsEverything(consumer.AdditionalProps) {
		return
	}
	// 消费者禁止额外字段，而生产者允许：不兼容。
	if isBoolFalse(consumer.AdditionalProps) {
		add(findings, apPointer, "additionalProperties",
			"生产者允许额外字段，但消费者禁止额外字段",
			map[string]any{"unexpectedField": "x"})
		return
	}
	// 消费者用 schema 约束额外字段。
	if !producer.HasAdditionalProps || acceptsEverything(producer.AdditionalProps) {
		// 生产者的额外字段完全不受限：必能造出消费者拒绝的值。
		add(findings, apPointer, "additionalProperties",
			"生产者的额外字段不受约束，消费者却限定了额外字段的 schema",
			map[string]any{"unexpectedField": exampleViolating(consumer.AdditionalProps)})
		return
	}
	// 双方都给出了额外字段的 schema：递归比较。
	checkSubset(producer.AdditionalProps, consumer.AdditionalProps, apPointer, findings)
}

// isBoolFalse 判断 schema 是否为布尔 false。
func isBoolFalse(s *schema.Schema) bool {
	return s != nil && s.Bool != nil && !*s.Bool
}

func checkArray(producer, consumer *schema.Schema, pointer string, findings *[]Finding) {
	if consumer.Items == nil {
		return
	}
	if producer.Items == nil {
		add(findings, pointer+"/items", "items",
			"生产者未约束数组元素，可能产生消费者不接受的元素",
			[]any{exampleViolating(consumer.Items)})
		return
	}
	checkSubset(producer.Items, consumer.Items, pointer+"/items", findings)
}

// exampleViolating 生成一个违反 s 的示例值（尽力而为）。
func exampleViolating(s *schema.Schema) any {
	if s.HasEnum && len(s.Enum) > 0 {
		return valueOutsideEnum(&schema.Schema{}, s.Enum)
	}
	if s.HasType {
		for _, t := range []string{schema.TypeString, schema.TypeNumber, schema.TypeBoolean, schema.TypeObject, schema.TypeArray, schema.TypeNull} {
			ok := false
			for _, ct := range s.Type {
				if ct == t || (t == schema.TypeInteger && ct == schema.TypeNumber) {
					ok = true
				}
			}
			if !ok {
				return exampleForType(t)
			}
		}
	}
	if s.Minimum != nil {
		return *s.Minimum - 1
	}
	return nil
}

// valueOutsideEnum 生成一个不在 enum 中、且尽量符合 producer 类型的值。
func valueOutsideEnum(producer *schema.Schema, enum []any) any {
	candidates := []any{"__other__", -99999.0, true, nil}
	if producer.HasType {
		switch producer.Type[0] {
		case schema.TypeString:
			candidates = []any{"__other__", "__other__2"}
		case schema.TypeNumber, schema.TypeInteger:
			candidates = []any{-99999.0, 99999.0, 0.5}
		case schema.TypeBoolean:
			candidates = []any{true, false}
		}
	}
	for _, c := range candidates {
		if !enumContains(enum, c) {
			return c
		}
	}
	return "__other__"
}

// exampleValue 生成一个满足 s 的示例值（尽力而为，用于反例构造）。
func exampleValue(s *schema.Schema) any {
	if s == nil {
		return nil
	}
	if s.Bool != nil {
		if *s.Bool {
			return nil
		}
		return nil
	}
	if s.HasEnum && len(s.Enum) > 0 {
		return s.Enum[0]
	}
	if !s.HasType {
		return nil
	}
	return exampleForType(s.Type[0])
}

func exampleForType(t string) any {
	switch t {
	case schema.TypeString:
		return "x"
	case schema.TypeNumber:
		return 1.5
	case schema.TypeInteger:
		return 1
	case schema.TypeBoolean:
		return true
	case schema.TypeObject:
		return map[string]any{}
	case schema.TypeArray:
		return []any{}
	default:
		return nil
	}
}

func add(findings *[]Finding, path, kind, msg string, example any) {
	*findings = append(*findings, Finding{
		Path:    path,
		Kind:    kind,
		Message: msg,
		Example: example,
	})
}

func containsString(list []string, s string) bool {
	for _, v := range list {
		if v == s {
			return true
		}
	}
	return false
}

func sortedKeys(m map[string]*schema.Schema) []string {
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	return keys
}
