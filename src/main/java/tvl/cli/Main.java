package tvl.cli;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Paths;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import tvl.core.TriBool;
import tvl.engine.RequestHandler;
import tvl.eval.Evaluator;
import tvl.core.Value;
import tvl.json.Json;
import tvl.parser.Expr;
import tvl.parser.Parser;
import tvl.parser.TypeChecker;

/**
 * 命令行入口（无网络服务，仅处理 JSON 请求文本）：
 *
 *  java tvl.cli.Main query [request.json]          不提供文件则从标准输入读取
 *  java tvl.cli.Main parse "expr"                  解析并输出 AST
 *  java tvl.cli.Main truth-table                  输出 NOT/AND/OR 穷举真值表
 *
 * 退出码：0 成功；2 请求/表达式错误；1 用法或 IO 错误。
 */
public final class Main {

    public static void main(String[] args) {
        if (args.length == 0) {
            usage();
            System.exit(1);
        }
        try {
            switch (args[0]) {
                case "query":
                    doQuery(args.length > 1 ? args[1] : null);
                    break;
                case "parse":
                    doParse(args);
                    break;
                case "truth-table":
                    doTruthTable();
                    break;
                case "--help":
                case "-h":
                case "help":
                    usage();
                    break;
                default:
                    System.err.println("未知子命令：" + args[0]);
                    usage();
                    System.exit(1);
            }
        } catch (IOException ex) {
            System.err.println("IO 错误：" + ex.getMessage());
            System.exit(1);
        }
    }

    private static void doQuery(String file) throws IOException {
        String request = file == null
                ? readAll(System.in)
                : new String(Files.readAllBytes(Paths.get(file)), StandardCharsets.UTF_8);

        RequestHandler handler = new RequestHandler();
        RequestHandler.Response resp = handler.handle(request);
        System.out.print(resp.toJson());
        System.out.flush();
        System.exit(resp.ok ? 0 : 2);
    }

    private static void doParse(String[] args) {
        if (args.length < 2) {
            System.err.println("用法：parse \"<表达式>\"");
            System.exit(1);
        }
        String source = args[1];
        // parse 只做词法/语法分析并输出 AST（无表结构，列类型无法解析）。
        // 含列名表达式的类型检查请走 query 子命令并提供 table。
        Expr ast = Parser.parse(source);
        System.out.println(Json.pretty(ast.toJson()));
    }

    /** 穷举三值真值表：NOT(3) + AND(9) + OR(9) = 21 行。 */
    private static void doTruthTable() {
        TriBool[] vals = {TriBool.TRUE, TriBool.UNKNOWN, TriBool.FALSE};
        Evaluator ev = new Evaluator(new String[0]);

        List<Map<String, Object>> notRows = new ArrayList<>();
        for (TriBool a : vals) {
            Map<String, Object> r = new LinkedHashMap<>();
            r.put("a", a.name());
            r.put("NOT_a", a.not().name());
            notRows.add(r);
        }

        List<Map<String, Object>> andRows = new ArrayList<>();
        List<Map<String, Object>> orRows = new ArrayList<>();
        for (TriBool a : vals) {
            for (TriBool b : vals) {
                Map<String, Object> ra = new LinkedHashMap<>();
                ra.put("a", a.name());
                ra.put("b", b.name());
                ra.put("a_AND_b", a.and(b).name());
                andRows.add(ra);

                Map<String, Object> ro = new LinkedHashMap<>();
                ro.put("a", a.name());
                ro.put("b", b.name());
                ro.put("a_OR_b", a.or(b).name());
                orRows.add(ro);
            }
        }

        // 同时用真实表达式引擎验证一遍，确保表不是手写的
        verifyTruthTablesByEngine(ev);

        Map<String, Object> out = new LinkedHashMap<>();
        out.put("logic", "Kleene SQL 3VL (TRUE/FALSE/UNKNOWN)");
        out.put("NOT", notRows);
        out.put("AND", andRows);
        out.put("OR", orRows);
        System.out.println(Json.pretty(out));
    }

    /**
     * 用表达式 a AND b / a OR B / NOT a 对 9+9+3 种取值实际求值，
     * 与枚举表核对（a、b 是用布尔字面量经 NULL 构造的逻辑值）。
     */
    private static void verifyTruthTablesByEngine(Evaluator ev) {
        // 该方法为内部一致性断言；真正的穷举测试在测试套件中进行。
        Value t = Value.ofBoolean(true);
        Value f = Value.ofBoolean(false);
        Value u = Value.NULL;
        Value[] vals = {t, u, f};
        TriBool[] tris = {TriBool.TRUE, TriBool.UNKNOWN, TriBool.FALSE};

        for (int i = 0; i < 3; i++) {
            if (tris[i].not() != notByEval(ev, vals[i])) {
                throw new AssertionError("NOT 真值表引擎不一致");
            }
            for (int j = 0; j < 3; j++) {
                if (tris[i].and(tris[j]) != andByEval(ev, vals[i], vals[j])) {
                    throw new AssertionError("AND 真值表引擎不一致");
                }
                if (tris[i].or(tris[j]) != orByEval(ev, vals[i], vals[j])) {
                    throw new AssertionError("OR 真值表引擎不一致");
                }
            }
        }
    }

    private static TriBool notByEval(Evaluator ev, Value a) {
        // 构造 NOT TRUE / NOT FALSE / NOT NULL 常量表达式实际求值
        String operand = a.isNull() ? "NULL"
                : (a.bool() ? "TRUE" : "FALSE");
        Expr e = Parser.parse("NOT " + operand);
        TypeChecker.checkStandalone(e);
        return ev.evalLogic(e, new Value[0]);
    }

    private static TriBool andByEval(Evaluator ev, Value a, Value b) {
        String la = a.isNull() ? "NULL" : (a.bool() ? "TRUE" : "FALSE");
        String lb = b.isNull() ? "NULL" : (b.bool() ? "TRUE" : "FALSE");
        Expr e = Parser.parse(la + " AND " + lb);
        TypeChecker.checkStandalone(e);
        return ev.evalLogic(e, new Value[0]);
    }

    private static TriBool orByEval(Evaluator ev, Value a, Value b) {
        String la = a.isNull() ? "NULL" : (a.bool() ? "TRUE" : "FALSE");
        String lb = b.isNull() ? "NULL" : (b.bool() ? "TRUE" : "FALSE");
        Expr e = Parser.parse(la + " OR " + lb);
        TypeChecker.checkStandalone(e);
        return ev.evalLogic(e, new Value[0]);
    }

    private static String readAll(java.io.InputStream in) throws IOException {
        byte[] data = in.readAllBytes();
        return new String(data, StandardCharsets.UTF_8);
    }

    private static void usage() {
        System.err.println("用法：");
        System.err.println("  query [request.json]   执行 JSON 查询请求（缺省文件则读标准输入）");
        System.err.println("  parse \"<expr>\"          解析表达式并输出 AST JSON");
        System.err.println("  truth-table            输出 NOT/AND/OR 穷举三值真值表");
    }
}
