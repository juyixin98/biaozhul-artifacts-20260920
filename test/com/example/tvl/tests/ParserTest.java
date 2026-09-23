package com.example.tvl.tests;

import com.example.tvl.engine.QueryService;
import com.example.tvl.engine.Schema;
import com.example.tvl.sql.Ast;
import com.example.tvl.sql.Ast.BoolExpr;
import com.example.tvl.sql.Ast.NotExpr;
import com.example.tvl.sql.Ast.Query;
import com.example.tvl.sql.SqlParser;
import com.example.tvl.sql.SqlParseException;
import com.example.tvl.sql.DataType;

import java.util.List;

import static com.example.tvl.tests.MiniTest.assertEquals;
import static com.example.tvl.tests.MiniTest.assertThrows;


/** SQL 解析：结构、参数编号、括号优先级、错误输入。 */
public final class ParserTest {

    private static Schema schema2() {
        return new Schema(List.of(
                new Schema.Column("a", DataType.INTEGER),
                new Schema.Column("b", DataType.INTEGER)));
    }

    public static MiniTest.Suite suite() {
        QueryService service = new QueryService();
        return MiniTest.suite("SQL 解析器 / 括号优先级")

                .test("SELECT * 与 WHERE 缺省", () -> {
                    Query q = SqlParser.parse("select * from t");
                    assertEquals(true, q.selectAll(), "selectAll");
                    assertEquals(null, q.where(), "无 where");
                })

                .test("关键字大小写不敏感，列名保留", () -> {
                    Query q = SqlParser.parse("SeLeCt A FROM MyTable WhErE A = 1");
                    assertEquals("A", q.columns().get(0), "列名原样保留");
                    assertEquals("MyTable", q.table(), "表名原样保留");
                })

                .test("AND 优先级高于 OR：a OR b AND c 解析为 a OR (b AND c)", () -> {
                    Query q = SqlParser.parse("SELECT a FROM t WHERE a = 1 OR b = 2 AND a = 3");
                    assertTrue(q.where() instanceof BoolExpr or && !or.conjunction(), "顶层为 OR");
                    BoolExpr or = (BoolExpr) q.where();
                    assertEquals(2, or.terms().size(), "OR 两个分支");
                    assertTrue(or.terms().get(1) instanceof BoolExpr and && and.conjunction(),
                            "右分支是 AND");
                })

                .test("括号改变优先级：(a OR b) AND c", () -> {
                    Query q = SqlParser.parse("SELECT a FROM t WHERE (a = 1 OR b = 2) AND a = 3");
                    assertTrue(q.where() instanceof BoolExpr and && and.conjunction(), "顶层为 AND");
                    BoolExpr and = (BoolExpr) q.where();
                    assertTrue(and.terms().get(0) instanceof BoolExpr or && !or.conjunction(),
                            "左分支是括号内 OR");
                })

                .test("嵌套 NOT 与括号", () -> {
                    Query q = SqlParser.parse("SELECT a FROM t WHERE NOT NOT (a = 1)");
                    assertTrue(q.where() instanceof NotExpr n1
                            && n1.operand() instanceof NotExpr, "双层 NOT");
                })

                .test("IS NULL 与 IS NOT NULL", () -> {
                    Query q1 = SqlParser.parse("SELECT a FROM t WHERE a IS NULL");
                    assertTrue(q1.where() instanceof Ast.IsNull isn && !isn.negated(), "IS NULL");
                    Query q2 = SqlParser.parse("SELECT a FROM t WHERE a IS NOT NULL");
                    assertTrue(q2.where() instanceof Ast.IsNull isn2 && isn2.negated(), "IS NOT NULL");
                })

                .test("参数按出现顺序从 0 编号", () -> {
                    Query q = SqlParser.parse("SELECT a FROM t WHERE a = ? AND b = ? OR a > ?");
                    assertEquals(3, SqlParser.paramCount("SELECT a FROM t WHERE a = ? AND b = ? OR a > ?"),
                            "参数总数 3");
                    // 编译成功即说明编号被消费
                    service.compile("SELECT a FROM t WHERE a = ? AND b = ? OR a > ?",
                            schema2(), List.of(
                                    java.util.Map.of("type", "INTEGER", "value", 1L),
                                    java.util.Map.of("type", "INTEGER", "value", 2L),
                                    java.util.Map.of("type", "INTEGER", "value", 3L)));
                })

                .test("字符串内双引号转义", () -> {
                    Query q = SqlParser.parse("SELECT a FROM t WHERE 'it''s' = 'it''s'");
                    // 两侧文本相等 → TRUE，过滤保留所有行
                    var c = service.compile("SELECT a FROM t WHERE 'it''s' = 'it''s'", schema2(), List.of());
                    var b = com.example.tvl.engine.Batch.fromRows(schema2(), List.of(List.of(1L, 2L)));
                    var r = service.executeBatch(c, b, false);
                    assertEquals(1, r.get("selected"), "转义字符串相等比较为 TRUE");
                })

                .test("错误：缺 FROM", () ->
                        assertThrows(SqlParseException.class, "FROM", () ->
                                SqlParser.parse("SELECT a")))

                .test("错误：括号不闭合", () ->
                        assertThrows(SqlParseException.class, "')'", () ->
                                SqlParser.parse("SELECT a FROM t WHERE (a = 1")))

                .test("错误：悬空运算符", () ->
                        assertThrows(SqlParseException.class, null, () ->
                                SqlParser.parse("SELECT a FROM t WHERE a =")))

                .test("错误：WHERE 后多余内容", () ->
                        assertThrows(SqlParseException.class, null, () ->
                                SqlParser.parse("SELECT a FROM t WHERE a = 1 GARBAGE")))

                .test("错误：不支持布尔字面量", () ->
                        assertThrows(SqlParseException.class, "布尔字面量", () ->
                                SqlParser.parse("SELECT a FROM t WHERE TRUE")))

                .test("错误：IS NULL 作用于括号表达式", () ->
                        assertThrows(SqlParseException.class, "IS NULL", () ->
                                SqlParser.parse("SELECT a FROM t WHERE (a = 1) IS NULL")))

                .test("容忍分号结尾与 <>、<=、>=", () -> {
                    service.compile("SELECT a FROM t WHERE a <> 1 AND b <= 2 AND a >= 0;",
                            schema2(), List.of());
                });
    }

    private static void assertTrue(boolean cond, String msg) {
        MiniTest.assertTrue(cond, msg);
    }
}
