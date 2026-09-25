package boolsearch;

import boolsearch.TestRunner.Case;
import boolsearch.query.Node;
import boolsearch.query.ParseException;
import boolsearch.query.Parser;

import java.util.List;

/** 解析器测试：优先级、括号、大小写、扁平化、错误位置。 */
public final class ParserTests {

    static void register(List<Case> cases) {
        cases.add(new Case("parser: 优先级 NOT>AND>OR", ParserTests::precedence));
        cases.add(new Case("parser: 括号覆盖优先级", ParserTests::parens));
        cases.add(new Case("parser: 同级扁平化", ParserTests::flatten));
        cases.add(new Case("parser: 关键字大小写不敏感/词项小写化", ParserTests::caseInsensitive));
        cases.add(new Case("parser: 连续 NOT", ParserTests::doubleNot));
        cases.add(new Case("parser: 错误位置保留", ParserTests::errorPositions));
    }

    static void precedence() throws Exception {
        Node n = Parser.parse("a OR b AND c");
        Check.isTrue(n instanceof Node.Or, "顶层应为 OR");
        Node.Or or = (Node.Or) n;
        Check.eq(or.children().size(), 2, "OR 子节点数");
        Check.isTrue(or.children().get(1) instanceof Node.And, "OR 右子应为 AND");

        Node m = Parser.parse("NOT a AND b");
        Check.isTrue(m instanceof Node.And, "顶层应为 AND");
        Check.isTrue(((Node.And) m).children().get(0) instanceof Node.Not, "NOT 应绑定最近词项");
    }

    static void parens() throws Exception {
        Node n = Parser.parse("(a OR b) AND c");
        Check.isTrue(n instanceof Node.And, "顶层应为 AND");
        Check.isTrue(((Node.And) n).children().get(0) instanceof Node.Or, "左子应为括号内 OR");
    }

    static void flatten() throws Exception {
        Node n = Parser.parse("a AND b AND c AND d");
        Check.isTrue(n instanceof Node.And, "顶层应为 AND");
        Check.eq(((Node.And) n).children().size(), 4, "AND 应扁平化为 4 个子节点");
        Node o = Parser.parse("a OR b OR c");
        Check.eq(((Node.Or) o).children().size(), 3, "OR 应扁平化为 3 个子节点");
    }

    static void caseInsensitive() throws Exception {
        Node n = Parser.parse("Apple aNd BANANA");
        Node.And and = (Node.And) n;
        Check.eq(((Node.Term) and.children().get(0)).term(), "apple", "词项应小写化");
        Check.eq(((Node.Term) and.children().get(1)).term(), "banana", "词项应小写化");
    }

    static void doubleNot() throws Exception {
        Node n = Parser.parse("NOT NOT apple");
        Check.isTrue(n instanceof Node.Not, "顶层应为 NOT");
        Check.isTrue(((Node.Not) n).child() instanceof Node.Not, "应嵌套 NOT");
    }

    static void errorPositions() {
        // {查询串, 期望错误位置}
        String[][] cases = {
                {"", "0"},            // 空查询：位置 0（EOF）
                {"a AND", "5"},       // AND 后缺词项：EOF 位置 = 串长
                {"(a OR b", "7"},     // 缺少 ')'
                {"a b", "2"},         // 缺少运算符
                {"AND a", "0"},       // 以运算符开头
                {"a & b", "2"},       // 非法字符
                {"a AND )", "6"},     // 意外右括号
                {"NOT", "3"},         // NOT 后无操作数
                {"a OR (b AND", "11"},// 括号未闭合（EOF）
        };
        for (String[] c : cases) {
            String q = c[0];
            int expected = Integer.parseInt(c[1]);
            try {
                Parser.parse(q);
                throw new AssertionError("查询应解析失败: '" + q + "'");
            } catch (ParseException e) {
                Check.eq(e.position(), expected,
                        "错误位置不符: '" + q + "' (" + e.getMessage() + ")");
            }
        }
    }
}
