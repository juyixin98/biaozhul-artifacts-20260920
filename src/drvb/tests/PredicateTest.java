package drvb.tests;

import drvb.json.Json;
import drvb.rule.Predicate;
import drvb.rule.Predicates;
import drvb.rule.RuleException;

import java.util.Map;

import static drvb.tests.Asserts.assertFalse;
import static drvb.tests.Asserts.assertThrows;
import static drvb.tests.Asserts.assertTrue;

public class PredicateTest {

    private static Map<String, Object> ctx(String json) {
        return Json.asObject(Json.parse(json));
    }

    private static Predicate p(String json) {
        return Predicates.compile(Json.asObject(Json.parse(json)));
    }

    @Test
    public void comparisonsAndNumericEquality() {
        assertTrue(p("{\"op\":\"gte\",\"field\":\"amount\",\"value\":100}")
                        .test(ctx("{\"amount\":100}")), "100>=100 边界相等为 true");
        assertFalse(p("{\"op\":\"gt\",\"field\":\"amount\",\"value\":100}")
                .test(ctx("{\"amount\":100}")), "100>100 为 false");
        assertTrue(p("{\"op\":\"eq\",\"field\":\"amount\",\"value\":100}")
                .test(ctx("{\"amount\":100.0}")), "数字数值相等 Long/Double 互通");
        assertFalse(p("{\"op\":\"lt\",\"field\":\"amount\",\"value\":50}")
                .test(ctx("{\"amount\":\"big\"}")), "类型不符 -> false 而非异常");
        assertFalse(p("{\"op\":\"gt\",\"field\":\"amount\",\"value\":50}")
                .test(ctx("{}")), "字段缺失 -> false");
    }

    @Test
    public void inContainsStringAndNestedPath() {
        assertTrue(p("{\"op\":\"in\",\"field\":\"tier\",\"values\":[\"gold\",\"vip\"]}")
                .test(ctx("{\"tier\":\"vip\"}")), "in 命中");
        assertFalse(p("{\"op\":\"notIn\",\"field\":\"tier\",\"values\":[\"gold\"]}")
                .test(ctx("{\"tier\":\"gold\"}")), "notIn 命中取反");
        assertTrue(p("{\"op\":\"startsWith\",\"field\":\"name\",\"value\":\"al\"}")
                .test(ctx("{\"name\":\"alice\"}")), "startsWith");
        assertTrue(p("{\"op\":\"contains\",\"field\":\"u.tag\",\"value\":\"x\"}")
                .test(ctx("{\"u\":{\"tag\":\"xyz\"}}")), "点分嵌套路径");
        assertTrue(p("{\"op\":\"isnull\",\"field\":\"missing\"}").test(ctx("{}")),
                "缺失字段 isnull");
    }

    @Test
    public void booleanCombinatorsAndNot() {
        Predicate and = p("{\"op\":\"and\",\"predicates\":["
                + "{\"op\":\"gte\",\"field\":\"a\",\"value\":1},"
                + "{\"op\":\"lt\",\"field\":\"b\",\"value\":5}]}");
        assertTrue(and.test(ctx("{\"a\":2,\"b\":3}")), "and 真");
        assertFalse(and.test(ctx("{\"a\":2,\"b\":9}")), "and 假");
        Predicate or = p("{\"op\":\"or\",\"predicates\":["
                + "{\"op\":\"eq\",\"field\":\"x\",\"value\":1},"
                + "{\"op\":\"eq\",\"field\":\"x\",\"value\":2}]}");
        assertTrue(or.test(ctx("{\"x\":2}")), "or 命中");
        Predicate not = p("{\"op\":\"not\",\"predicate\":"
                + "{\"op\":\"eq\",\"field\":\"x\",\"value\":1}}");
        assertTrue(not.test(ctx("{\"x\":2}")), "not 取反");
    }

    @Test
    public void reservedFieldsAreReachable() {
        // type/eventId/eventTime 由流处理器注入；这里直接模拟上下文
        assertTrue(p("{\"op\":\"eq\",\"field\":\"type\",\"value\":\"payment\"}")
                .test(ctx("{\"type\":\"payment\",\"amount\":1}")), "保名 type 可用");
    }

    @Test
    public void invalidSpecFailsFastAtPublish() {
        assertThrows(RuleException.class,
                () -> p("{\"op\":\"weird\",\"field\":\"x\"}"), "未知算子");
        assertThrows(RuleException.class,
                () -> p("{\"op\":\"and\",\"predicates\":[]}"), "空 and");
        assertThrows(RuleException.class,
                () -> p("{\"op\":\"gt\",\"field\":\"x\",\"value\":\"nope\"}"),
                "大小比较 value 非数字");
        assertThrows(RuleException.class,
                () -> p("{\"op\":\"in\",\"field\":\"x\"}"), "in 缺 values");
    }
}
