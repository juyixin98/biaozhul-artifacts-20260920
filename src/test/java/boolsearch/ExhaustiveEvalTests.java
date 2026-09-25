package boolsearch;

import boolsearch.TestRunner.Case;
import boolsearch.eval.Evaluator;
import boolsearch.query.Node;
import boolsearch.query.Parser;

import java.util.ArrayList;
import java.util.List;
import java.util.Random;
import java.util.SortedSet;
import java.util.TreeSet;

/**
 * 穷举对照测试：3 词词表 {a,b,c}，文档全集 = 全部 2^3=8 个子集（doc id 即位掩码）。
 * 参考语义：对每篇文档直接按布尔表达式求值（与索引无关的集合运算定义）。
 * 比较：朴素计划、优化计划、参考语义三者结果必须一致。
 * 另覆盖：删除文档后的全集变化、未知词、随机深度查询、AST 序列化往返。
 */
public final class ExhaustiveEvalTests {
    private static final String[] TERMS = {"a", "b", "c"};

    static void register(List<Case> cases) {
        cases.add(new Case("exhaustive: 8 文档全集 × 全部深度≤2 查询（朴素/优化/参考一致）",
                ExhaustiveEvalTests::exhaustive));
        cases.add(new Case("exhaustive: 删除文档后重查（全集收缩）", ExhaustiveEvalTests::afterDelete));
        cases.add(new Case("exhaustive: 2000 条随机深度查询（含未知词）", ExhaustiveEvalTests::randomDeep));
        cases.add(new Case("exhaustive: AST 序列化→再解析→求值一致", ExhaustiveEvalTests::roundTrip));
    }

    /** 构造 8 篇文档：doc i 含词 t_k 当且仅当第 k 位置位。 */
    private static InvertedIndex buildIndex() {
        InvertedIndex index = new InvertedIndex();
        for (int i = 0; i < 8; i++) {
            StringBuilder sb = new StringBuilder();
            for (int k = 0; k < 3; k++) {
                if (((i >> k) & 1) == 1) {
                    if (sb.length() > 0) sb.append(' ');
                    sb.append(TERMS[k]);
                }
            }
            index.addDocument(i, sb.toString());
        }
        return index;
    }

    /** 参考语义：逐文档布尔求值。 */
    private static boolean refEval(Node n, int doc) {
        if (n instanceof Node.Term t) {
            for (int k = 0; k < 3; k++) {
                if (TERMS[k].equals(t.term())) return ((doc >> k) & 1) == 1;
            }
            return false; // 未知词
        }
        if (n instanceof Node.Not x) return !refEval(x.child(), doc);
        if (n instanceof Node.And a) {
            for (Node c : a.children()) if (!refEval(c, doc)) return false;
            return true;
        }
        if (n instanceof Node.Or o) {
            for (Node c : o.children()) if (refEval(c, doc)) return true;
            return false;
        }
        throw new IllegalStateException();
    }

    private static SortedSet<Integer> reference(Node n, SortedSet<Integer> universe) {
        TreeSet<Integer> r = new TreeSet<>();
        for (int d : universe) if (refEval(n, d)) r.add(d);
        return r;
    }

    /** 由 prev 层生成下一层：NOT(x)、AND(x,y)、OR(x,y)。 */
    private static List<Node> nextLevel(List<Node> prev) {
        List<Node> out = new ArrayList<>(prev);
        for (Node x : prev) out.add(new Node.Not(x));
        for (Node x : prev) {
            for (Node y : prev) {
                out.add(new Node.And(List.of(x, y)));
                out.add(new Node.Or(List.of(x, y)));
            }
        }
        return out;
    }

    private static void checkAll(InvertedIndex index, List<Node> queries, String tag) {
        Evaluator naive = new Evaluator(index, false);
        Evaluator opt = new Evaluator(index, true);
        for (Node q : queries) {
            SortedSet<Integer> ref = reference(q, index.universe());
            SortedSet<Integer> rn = naive.eval(q);
            SortedSet<Integer> ro = opt.eval(q);
            Check.eq(rn, ref, tag + " 朴素计划与参考不一致: " + q);
            Check.eq(ro, ref, tag + " 优化计划与参考不一致: " + q);
        }
    }

    static void exhaustive() {
        InvertedIndex index = buildIndex();
        List<Node> s0 = List.of(new Node.Term("a"), new Node.Term("b"), new Node.Term("c"));
        List<Node> s1 = nextLevel(s0);           // 24 条
        List<Node> s2 = nextLevel(s1);           // 1200 条
        List<Node> all = new ArrayList<>();
        all.addAll(s0);
        all.addAll(s1);
        all.addAll(s2);
        checkAll(index, all, "穷举");
        System.out.println("    （共 " + all.size() + " 条查询 × 2 种计划 × 参考对照）");
    }

    static void afterDelete() {
        InvertedIndex index = buildIndex();
        Check.isTrue(index.deleteDocument(3), "删除 doc 3 应成功");
        Check.isTrue(index.deleteDocument(5), "删除 doc 5 应成功");
        Check.eq(index.universe().size(), 6, "全集应收缩为 6");
        List<Node> s0 = List.of(new Node.Term("a"), new Node.Term("b"), new Node.Term("c"));
        List<Node> s1 = nextLevel(s0);
        List<Node> all = new ArrayList<>(s0);
        all.addAll(s1);
        checkAll(index, all, "删除后");
    }

    private static Node randomQuery(Random r, int depth) {
        String[] vocab = {"a", "b", "c", "z"}; // z 为未知词
        if (depth == 0 || r.nextInt(3) == 0) {
            return new Node.Term(vocab[r.nextInt(vocab.length)]);
        }
        return switch (r.nextInt(3)) {
            case 0 -> new Node.Not(randomQuery(r, depth - 1));
            case 1 -> new Node.And(List.of(randomQuery(r, depth - 1), randomQuery(r, depth - 1)));
            default -> new Node.Or(List.of(randomQuery(r, depth - 1), randomQuery(r, depth - 1)));
        };
    }

    static void randomDeep() {
        InvertedIndex index = buildIndex();
        Random r = new Random(20260922L);
        List<Node> queries = new ArrayList<>();
        for (int i = 0; i < 2000; i++) queries.add(randomQuery(r, 5));
        checkAll(index, queries, "随机深度");
    }

    /** AST -> 查询串 -> 再解析 -> 两种计划求值一致。 */
    static String render(Node n) {
        if (n instanceof Node.Term t) return t.term();
        if (n instanceof Node.Not x) return "NOT " + render(x.child());
        if (n instanceof Node.And a) {
            StringBuilder sb = new StringBuilder("(");
            for (int i = 0; i < a.children().size(); i++) {
                if (i > 0) sb.append(" AND ");
                sb.append(render(a.children().get(i)));
            }
            return sb.append(')').toString();
        }
        Node.Or o = (Node.Or) n;
        StringBuilder sb = new StringBuilder("(");
        for (int i = 0; i < o.children().size(); i++) {
            if (i > 0) sb.append(" OR ");
            sb.append(render(o.children().get(i)));
        }
        return sb.append(')').toString();
    }

    static void roundTrip() throws Exception {
        InvertedIndex index = buildIndex();
        Random r = new Random(7L);
        Evaluator opt = new Evaluator(index, true);
        for (int i = 0; i < 500; i++) {
            Node q = randomQuery(r, 4);
            Node reparsed = Parser.parse(render(q));
            Check.eq(opt.eval(reparsed), opt.eval(q), "序列化往返后结果不一致: " + render(q));
        }
    }
}
