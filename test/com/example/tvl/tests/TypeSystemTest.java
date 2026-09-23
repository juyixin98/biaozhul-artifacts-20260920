package com.example.tvl.tests;

import com.example.tvl.engine.Batch;
import com.example.tvl.engine.QueryService;
import com.example.tvl.engine.Schema;
import com.example.tvl.engine.SemanticException;
import com.example.tvl.sql.DataType;
import com.example.tvl.sql.SqlParseException;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import static com.example.tvl.tests.MiniTest.assertEquals;
import static com.example.tvl.tests.MiniTest.assertThrows;
import static com.example.tvl.tests.MiniTest.assertTrue;


/** 类型检查与参数绑定：拒绝隐式字符串↔数值混转，参数数量/类型严格。 */
public final class TypeSystemTest {

    private static Schema schema3() {
        return new Schema(List.of(
                new Schema.Column("id", DataType.INTEGER),
                new Schema.Column("score", DataType.FLOAT),
                new Schema.Column("name", DataType.TEXT)));
    }

    private static Map<String, Object> binding(String type, Object value) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("type", type);
        m.put("value", value);
        return m;
    }

    public static MiniTest.Suite suite() {
        QueryService service = new QueryService();
        return MiniTest.suite("类型检查 / 参数绑定 / 拒绝隐式混转")

                .test("列 文本与整数比较被拒绝", () ->
                        assertThrows(SemanticException.class, "类型不匹配", () ->
                                service.compile("SELECT id FROM t WHERE name = id",
                                        schema3(), List.of())))

                .test("文本列与整型字面量比较被拒绝", () ->
                        assertThrows(SemanticException.class, "类型不匹配", () ->
                                service.compile("SELECT id FROM t WHERE name = 3",
                                        schema3(), List.of())))

                .test("整型列与字符串字面量比较被拒绝", () ->
                        assertThrows(SemanticException.class, "类型不匹配", () ->
                                service.compile("SELECT id FROM t WHERE id = '3'",
                                        schema3(), List.of())))

                .test("INTEGER 与 FLOAT 数值列允许比较（数值内部宽化）", () ->
                        service.compile("SELECT id FROM t WHERE id >= score", schema3(), List.of()))

                .test("数值列与文本参数比较被拒绝", () ->
                        assertThrows(SemanticException.class, "类型不匹配", () ->
                                service.compile("SELECT id FROM t WHERE name >= ?",
                                        schema3(), List.of(binding("INTEGER", 1L)))))

                .test("整型列与声明为 TEXT 的参数比较被拒绝", () ->
                        assertThrows(SemanticException.class, "类型不匹配", () ->
                                service.compile("SELECT id FROM t WHERE id = ?",
                                        schema3(), List.of(binding("TEXT", "1")))))

                .test("数据写入：字符串进入 INTEGER 列被拒绝", () ->
                        assertThrows(SemanticException.class, "拒绝隐式字符串转数值", () ->
                                Batch.fromRows(schema3(), List.of(java.util.Arrays.asList("1", 1.0, "a")))))

                .test("数据写入：数字进入 TEXT 列被拒绝", () ->
                        assertThrows(SemanticException.class, "拒绝隐式数值转字符串", () ->
                                Batch.fromRows(schema3(), List.of(List.of(1L, 1.0, 42L)))))

                .test("参数：需要 2 个只给 1 个报错", () ->
                        assertThrows(SemanticException.class, "参数数量不匹配", () ->
                                service.compile("SELECT id FROM t WHERE id = ? AND score > ?",
                                        schema3(), List.of(binding("INTEGER", 1L)))))

                .test("参数：多出绑定值报错", () ->
                        assertThrows(SemanticException.class, "参数数量不匹配", () ->
                                service.compile("SELECT id FROM t WHERE id = ?",
                                        schema3(), List.of(
                                                binding("INTEGER", 1L),
                                                binding("INTEGER", 2L)))))

                .test("参数：INTEGER 收到 JSON 字符串被拒绝", () ->
                        assertThrows(SemanticException.class, "拒绝隐式字符串转数值", () ->
                                service.compile("SELECT id FROM t WHERE id = ?",
                                        schema3(), List.of(binding("INTEGER", "7")))))

                .test("参数：FLOAT 收到 JSON 字符串被拒绝", () ->
                        assertThrows(SemanticException.class, "拒绝隐式字符串转数值", () ->
                                service.compile("SELECT id FROM t WHERE score = ?",
                                        schema3(), List.of(binding("FLOAT", "1.5")))))

                .test("参数：INTEGER 收到带小数数字被拒绝", () ->
                        assertThrows(SemanticException.class, "不是整数", () ->
                                service.compile("SELECT id FROM t WHERE id = ?",
                                        schema3(), List.of(binding("INTEGER", 1.5d)))))

                .test("参数：TEXT 收到 JSON 数字被拒绝", () ->
                        assertThrows(SemanticException.class, "拒绝隐式数值转字符串", () ->
                                service.compile("SELECT id FROM t WHERE name = ?",
                                        schema3(), List.of(binding("TEXT", 7L)))))

                .test("参数：声明类型非法报错", () ->
                        assertThrows(SemanticException.class, "类型非法", () ->
                                service.compile("SELECT id FROM t WHERE id = ?",
                                        schema3(), List.of(binding("BOOLEAN", true)))))

                .test("参数：null 值按声明类型绑定且比较为 UNKNOWN", () -> {
                    QueryService.Compiled c = service.compile(
                            "SELECT id FROM t WHERE id = ?", schema3(),
                            List.of(binding("INTEGER", null)));
                    Batch b = Batch.fromRows(schema3(), List.of(
                            List.of(1L, 1.0, "a"),
                            java.util.Arrays.asList(null, null, null)));
                    byte[] vec = com.example.tvl.engine.VectorExecutor.evalWhere(
                            c.plan().where(), b, c.params());
                    assertEquals(2, vec[0], "非NULL列 vs NULL参数 → UNKNOWN(2)");
                    assertEquals(2, vec[1], "NULL列 vs NULL参数 → UNKNOWN(2)");
                })

                .test("裸 NULL 与整型列比较合法且恒 UNKNOWN", () -> {
                    QueryService.Compiled c = service.compile(
                            "SELECT id FROM t WHERE id = NULL", schema3(), List.of());
                    Batch b = Batch.fromRows(schema3(), List.of(List.of(1L, 1.0, "a")));
                    byte[] vec = com.example.tvl.engine.VectorExecutor.evalWhere(
                            c.plan().where(), b, c.params());
                    assertEquals(2, vec[0], "= NULL 恒 UNKNOWN");
                })

                .test("未知列报错", () ->
                        assertThrows(SemanticException.class, "未知列", () ->
                                service.compile("SELECT nope FROM t", schema3(), List.of())))

                .test("FLOAT 列拒绝 NaN 数据", () ->
                        assertThrows(SemanticException.class, "有限", () ->
                                Batch.fromRows(schema3(), List.of(List.of(1L, Double.NaN, "a")))));
    }
}
