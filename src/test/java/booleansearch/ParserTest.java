package booleansearch;

import booleansearch.query.QueryNode;
import booleansearch.query.QueryParseException;
import booleansearch.query.QueryParser;
import booleansearch.search.AstRendererPackageTestHook;

import static booleansearch.TestFramework.assertEquals;
import static booleansearch.TestFramework.assertTrue;

/**
 * 解析器测试：
 * 1) 合法查询的 AST 结构（通过规范化渲染文本断言）；
 * 2) 各类语法错误必须抛出异常且 position 精确（保留错误位置）。
 */
public final class ParserTest {

    public static void run() {
        TestFramework.reset();
        TestFramework.section("ParserTest: 合法查询的 AST 规范化");

        assertAst("cat", "cat");
        assertAst("cat AND dog", "(cat AND dog)");
        assertAst("cat AND dog OR pet", "((cat AND dog) OR pet)");
        assertAst("cat OR dog AND pet", "(cat OR (dog AND pet))");
        assertAst("NOT cat", "(NOT cat)");
        assertAst("NOT NOT cat", "(NOT (NOT cat))");
        assertAst("NOT cat AND dog", "((NOT cat) AND dog)");
        assertAst("cat AND NOT dog OR NOT pet", "((cat AND (NOT dog)) OR (NOT pet))");
        assertAst("(cat OR dog) AND pet", "((cat OR dog) AND pet)");
        assertAst("((cat))", "cat");
        assertAst("CAT and Dog OR noT food", "((cat AND dog) OR (NOT food))");
        assertAst("cat & dog | !pet", "((cat AND dog) OR (NOT pet))");
        assertAst("（cat OR dog）AND pet", "((cat OR dog) AND pet)");
        assertAst("term_123 AND x", "(term_123 AND x)");

        TestFramework.section("ParserTest: 错误位置精确保留");

        // 空查询
        assertError("", 0);
        assertError("   ", 0);
        // 缺右括号：错误指向结尾 EOF 位置（串长）
        assertError("(cat", 4);
        assertError("(cat AND dog", 12);
        // 左操作数缺失：AND 位置
        assertError("AND cat", 0);
        assertError("cat OR", 6);
        assertError("cat AND AND dog", 8);
        // 右操作数缺失
        assertError("cat AND", 7);
        assertError("NOT", 3);
        assertError("NOT AND cat", 4);
        // 括号内为空，位置指向 '('
        assertError("()", 0);
        assertError("cat AND ()", 8);
        // 多余右括号
        assertError("cat)", 3);
        assertError("(cat))", 5);
        // 非法字符（引号 / @ / 中文词字，因为分词器仅收字母数字）
        assertError("cat \"dog", 4);
        assertError("cat@", 3);
        assertError("猫", 0);
        // NOT 后面是括号但括号为空
        assertError("NOT ()", 4);

        boolean ok = TestFramework.finish();
        if (!ok) {
            throw new AssertionError("ParserTest 存在失败");
        }
    }

    private static void assertAst(String query, String expectedRendered) {
        QueryNode ast = QueryParser.parse(query);
        String rendered = AstRendererPackageTestHook.render(ast);
        assertEquals(expectedRendered, rendered, "查询 \"" + query + "\" 的 AST");
    }

    private static void assertError(String query, int expectedPosition) {
        try {
            QueryParser.parse(query);
            assertTrue(false, "查询 \"" + query + "\" 应当抛出 QueryParseException");
        } catch (QueryParseException e) {
            assertEquals(expectedPosition, e.position(),
                    "查询 \"" + query + "\" 的错误位置 -> " + e.getMessage());
        }
    }
}
