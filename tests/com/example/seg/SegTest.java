package com.example.seg;

import com.example.seg.dict.Dictionary;
import com.example.seg.model.Costs;
import com.example.seg.model.SegPath;
import com.example.seg.model.Token;
import com.example.seg.seg.DpSegmenter;
import com.example.seg.seg.ExhaustiveSegmenter;
import com.example.seg.seg.Lattice;

import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Set;

/**
 * 分词核心测试：重叠词、未知字符、空串、同分决胜、N 最佳、穷举对照。
 */
public final class SegTest extends TestCase {

    public static void main(String[] args) throws Exception {
        System.exit(TestCase.run(new SegTest()));
    }

    // 受控测试词典（编程构造，不依赖数据文件）：
    //   研究1 研究生4 生命1 命4 生1
    //   结婚2 未婚2 尚未1.5 的1 和1
    //   学2 习5 学习3 工作4 工6 作6（用于同分决胜）
    // 未知单字统一代价 5。
    private final Dictionary dict = Dictionary.builder("test", Costs.ofInt(5))
            .description("受控测试词典")
            .add("研究", "1")
            .add("研究生", "4")
            .add("生命", "1")
            .add("命", "4")
            .add("生", "1")
            .add("结婚", "2")
            .add("未婚", "2")
            .add("尚未", "1.5")
            .add("的", "1")
            .add("和", "1")
            .add("学", "2")
            .add("习", "5")
            .add("学习", "3")
            .add("工作", "4")
            .add("工", "6")
            .add("作", "6")
            .build();

    private final DpSegmenter dp = new DpSegmenter();
    private final ExhaustiveSegmenter ex = new ExhaustiveSegmenter();

    @Override
    protected void run() {
        testBestBasic();
        testOverlappingWords();
        testUnknownChars();
        testEmptyString();
        testTieBreakFewerTokens();
        testTieBreakLexicographic();
        testNbestOrderingAndCoverage();
        testNbestKLimits();
        testNbestNoDuplicatePaths();
        testCostExactness();
        testExhaustiveAgreesWithDp();
        testExhaustiveLengthGuard();
        testCoveragePropertyOnCorpus();
        testLatticeEdges();
    }

    private void testBestBasic() {
        test("最小代价分词：研究/生/生命 代价3", () -> {
            SegPath p = dp.segment("研究生生命", dict);
            checkEq(p.joined(), "研究/生/生命", "最优切分");
            checkEq(p.cost(), Costs.ofInt(3), "总代价");
        });
    }

    private void testOverlappingWords() {
        test("重叠词：结婚的和尚未结婚的", () -> {
            SegPath p = dp.segment("结婚的和尚未结婚的", dict);
            checkEq(p.joined(), "结婚/的/和/尚未/结婚/的", "应识别 结婚 与 尚未");
            // 总代价 2+1+1+1.5+2+1 = 8.5
            checkEq(p.cost(), Costs.parseScaled("8.5"), "总代价");
        });
    }

    private void testUnknownChars() {
        test("未知字符：按单字代价计入，标记 known=false", () -> {
            SegPath p = dp.segment("研究X", dict);
            checkEq(p.joined(), "研究/X", "切分");
            checkEq(p.cost(), Costs.ofInt(1) + Costs.ofInt(5), "代价=研究1+未知5");
            Token x = p.tokens().get(1);
            check(!x.known(), "X 应标记为未知");
            checkEq(x.cost(), Costs.ofInt(5), "未知字代价");
            check(x.dictCost() == null, "未知字没有声明代价");
        });

        test("连续未知字符：逐字成边，不合并成多字词", () -> {
            SegPath p = dp.segment("YZ", dict);
            checkEq(p.tokens().size(), 2, "两个未知单元");
            checkEq(p.cost(), Costs.ofInt(10), "5+5=10");
        });

        test("已知单字词与未知边互斥：'和' 命中词典不加未知边", () -> {
            Lattice lattice = Lattice.build("和", dict);
            long knownEdges = lattice.incomingAt(1).stream().filter(Lattice.Edge::known).count();
            long unknownEdges = lattice.incomingAt(1).stream().filter(e -> !e.known()).count();
            checkEq(knownEdges, 1L, "恰好一条已知边");
            checkEq(unknownEdges, 0L, "没有重复的未知边");
        });
    }

    private void testEmptyString() {
        test("空串：返回一条空路径，代价0，无token", () -> {
            SegPath p = dp.segment("", dict);
            checkEq(p.tokens().size(), 0, "无 token");
            checkEq(p.cost(), 0L, "代价 0");
            List<SegPath> nb = dp.nbest("", dict, 5);
            checkEq(nb.size(), 1, "空串 N 最佳也只有一条路径");
        });
    }

    private void testTieBreakFewerTokens() {
        test("同分决胜1：代价相同取 token 数少者", () -> {
            // "学习" 命中：学习(3)
            // 拆成 学(2)+习(5)=7，不同分；这里构造同分：
            // 学习工作 上： 学习(3)+工作(4)=7（2 token）
            // 学(2)+习(5)+工作(4)=11（3 token）→ 不同分，仅验证少 token 优先场景：
            // 用 "工X" 无法造同分；改为直接比较两条 SegPath 的 tieCompare
            SegPath fewer = SegPath.of(List.of(
                    Token.unknown("aa", Costs.ofInt(2)),
                    Token.unknown("bb", Costs.ofInt(2))), Costs.ofInt(4));
            SegPath more = SegPath.of(List.of(
                    Token.unknown("a", Costs.ofInt(1)),
                    Token.unknown("a", Costs.ofInt(1)),
                    Token.unknown("bb", Costs.ofInt(2))), Costs.ofInt(4));
            check(SegPath.tieCompare(fewer, more) < 0, "2 token 应排在 3 token 前");
            check(SegPath.tieCompare(more, fewer) > 0, "反向应 > 0");
        });
    }

    private void testTieBreakLexicographic() {
        test("同分决胜2：代价与token数相同取字面字典序", () -> {
            // "学习工作" 上两条 2-token 路径：
            //  学习(3)/工作(4) = 7
            //  学(2)/习工作? "习工作" 非词。需要构造词典场景：
            // 用程序临时词典： AB 与 AC 等长同代价
            Dictionary tie = Dictionary.builder("tie", Costs.ofInt(9))
                    .add("AB", "3").add("AC", "3")
                    .add("B", "1").add("C", "1").add("AX", "4")
                    .build();
            // 句子 "ABX"？改用 "AB"+"C" 与 "AC"+"B" 同覆盖句不存在。
            // 直接对 SegPath 做字典序决胜：
            SegPath p1 = SegPath.of(List.of(
                    Token.unknown("AB", Costs.ofInt(3)),
                    Token.unknown("D", Costs.ofInt(2))), Costs.ofInt(5));
            SegPath p2 = SegPath.of(List.of(
                    Token.unknown("AC", Costs.ofInt(3)),
                    Token.unknown("D", Costs.ofInt(2))), Costs.ofInt(5));
            check(SegPath.tieCompare(p1, p2) < 0, "AB 应排在 AC 前");
            check(tie != null, "临时词典可构造");
        });

        test("同分决胜在真实格上生效：学/习工作? —— 用真实等代价切分", () -> {
            // 受控词典中："学工作" 的切分：
            //  学(2)/工作(4)=6 （2 token, 首词"学"）
            // 没有第二条 2-token；构造：学习(3)/工(6)=9 vs 学(2)+工作(4)=6 不同分。
            // 采用临时等代价词典验证真实 DP 行为：
            Dictionary tie = Dictionary.builder("tie2", Costs.ofInt(9))
                    .add("甲乙", "2")   // 词
                    .add("甲", "1")     // 单字
                    .add("乙丙", "3")   // 与 甲乙/丙 同分需要：甲乙(2)+丙(9)=11 vs 甲(1)+乙丙(3)+?
                    .build();
            // 句 "甲乙丙"：
            //  甲乙(2)/丙(未知9)=11 （2 token）
            //  甲(1)/乙丙(3)=4      （2 token）-> 更优，非同分
            SegPath p = dp.segment("甲乙丙", tie);
            checkEq(p.joined(), "甲/乙丙", "最小代价路径");

            // 严格同分场景：词典让两条 2-token 路径同代价
            Dictionary eq = Dictionary.builder("tie3", Costs.ofInt(9))
                    .add("甲乙", "1").add("乙丙", "1")
                    .add("甲", "0").add("丙", "0")
                    .build();
            // 句 "甲乙丙"：
            //  甲乙(1)/丙(0)=1
            //  甲(0)/乙丙(1)=1  -> 同代价同 token 数，字典序 "甲乙" < "甲丙？" 比较首词后第二词：
            //  路径A=[甲乙,丙]，路径B=[甲,乙丙]，比较第一个 token："甲" < "甲乙"
            //  （"甲"是"甲乙"的前缀，短字符串更小），所以 B=[甲,乙丙] 应排前。
            SegPath best = dp.segment("甲乙丙", eq);
            checkEq(best.joined(), "甲/乙丙", "同分时按 token 序列字典序决胜");
            checkEq(best.cost(), Costs.ofInt(1), "代价仍为 1");
        });
    }

    private void testNbestOrderingAndCoverage() {
        test("N最佳：有序、单调不降、与穷举前K完全一致", () -> {
            for (String text : new String[]{"研究生生命", "结婚的和尚未结婚的", "研究生命X", "命"}) {
                List<SegPath> all = ex.enumerate(text, dict);
                List<SegPath> nb = dp.nbest(text, dict, 64);
                checkEq(nb.size(), all.size(),
                        "k>=路径总数时应返回全部路径: " + text);
                for (int i = 0; i < all.size(); i++) {
                    checkEq(nb.get(i).joined(), all.get(i).joined(),
                            "第 " + (i + 1) + " 路径一致: " + text);
                    checkEq(nb.get(i).cost(), all.get(i).cost(),
                            "第 " + (i + 1) + " 路径代价一致: " + text);
                }
                for (int i = 1; i < nb.size(); i++) {
                    check(SegPath.tieCompare(nb.get(i - 1), nb.get(i)) <= 0,
                            "N最佳必须单调有序: " + text);
                }
            }
        });
    }

    private void testNbestKLimits() {
        test("N最佳：k=1 只有最优；k 小于路径总数时只返回 k 条", () -> {
            checkEq(dp.nbest("研究生生命", dict, 1).size(), 1, "k=1");
            checkEq(dp.nbest("研究生生命", dict, 2).size(), 2, "k=2");
            checkEq(dp.nbest("研究生生命", dict, 64).size(),
                    ex.enumerate("研究生生命", dict).size(), "k=64 截断到总数");
        });

        test("N最佳：k 非法抛异常", () -> {
            boolean thrown = false;
            try {
                dp.nbest("命", dict, 0);
            } catch (IllegalArgumentException e) {
                thrown = true;
            }
            check(thrown, "k=0 应抛 IllegalArgumentException");
        });
    }

    private void testNbestNoDuplicatePaths() {
        test("N最佳：路径互不相同", () -> {
            List<SegPath> nb = dp.nbest("结婚的和尚未结婚的", dict, 64);
            Set<String> sig = new HashSet<>();
            for (SegPath p : nb) {
                check(sig.add(p.joined()), "重复路径: " + p.joined());
            }
        });
    }

    private void testCostExactness() {
        test("代价精确：6位小数用放大整数比较，无浮点误差", () -> {
            Dictionary d = Dictionary.builder("frac", Costs.ofInt(7))
                    .add("甲乙", "0.1").add("乙", "0.2").add("甲", "0.3").build();
            // 句 "甲乙"：甲乙=0.1 vs 甲0.3+乙0.2=0.5
            SegPath p = dp.segment("甲乙", d);
            checkEq(p.joined(), "甲乙", "0.1 的词应胜出");
            checkEq(p.cost(), Costs.parseScaled("0.1"), "精确 0.1");
        });
    }

    private void testExhaustiveAgreesWithDp() {
        test("穷举对照：多条小句上 DP 的前 K 与穷举逐一相同", () -> {
            String[] sentences = {
                    "研究生生命", "结婚的", "尚未结婚的", "和尚未",
                    "研究生", "生命命", "研究生命X", "结婚的和", "", "命", "的和的",
            };
            for (String text : sentences) {
                List<SegPath> all = ex.enumerate(text, dict);
                List<SegPath> nb = dp.nbest(text, dict, 10);
                int n = Math.min(10, all.size());
                checkEq(nb.size(), Math.max(1, n), "返回条数: '" + text + "'");
                for (int i = 0; i < n; i++) {
                    checkEq(nb.get(i).joined(), all.get(i).joined(),
                            "穷举vs DP 第" + (i + 1) + "条: '" + text + "'");
                    checkEq(nb.get(i).cost(), all.get(i).cost(),
                            "代价一致: '" + text + "'");
                }
            }
        });

        test("穷举：每条路径恰好覆盖原文且总代价=各token之和", () -> {
            for (String text : new String[]{"研究生生命", "结婚的和X", "命命"}) {
                for (SegPath p : ex.enumerate(text, dict)) {
                    StringBuilder concat = new StringBuilder();
                    long sum = 0;
                    for (Token t : p.tokens()) {
                        concat.append(t.surface());
                        sum += t.cost();
                    }
                    checkEq(concat.toString(), text, "路径覆盖原文: " + p.joined());
                    checkEq(sum, p.cost(), "代价可加: " + p.joined());
                }
            }
        });
    }

    private void testExhaustiveLengthGuard() {
        test("穷举长度保护：超过 16 字拒绝", () -> {
            String tooLong = "甲乙丙丁戊己庚辛壬癸子丑寅卯辰巳午"; // 17 字
            boolean thrown = false;
            try {
                ex.enumerate(tooLong, dict);
            } catch (IllegalArgumentException e) {
                thrown = true;
            }
            check(thrown, "长句必须拒绝穷举");
        });
    }

    private void testCoveragePropertyOnCorpus() {
        test("性质测试：随机短句上 DP最优 == 穷举最优（覆盖 200+ 组合）", () -> {
            char[] chars = {'研', '究', '生', '命', '结', '婚', '的', '和', '尚', '未', 'X', 'Y'};
            java.util.Random rnd = new java.util.Random(42);
            int checked = 0;
            for (int iter = 0; iter < 300; iter++) {
                int len = rnd.nextInt(7); // 0..6
                StringBuilder sb = new StringBuilder();
                for (int i = 0; i < len; i++) {
                    sb.append(chars[rnd.nextInt(chars.length)]);
                }
                String text = sb.toString();
                SegPath a = dp.segment(text, dict);
                SegPath b = ex.enumerate(text, dict).get(0);
                checkEq(a.joined(), b.joined(), "最优一致: '" + text + "'");
                checkEq(a.cost(), b.cost(), "最优代价一致: '" + text + "'");
                checked++;
            }
            check(checked == 300, "实际检查句数");
        });
    }

    private void testLatticeEdges() {
        test("格：边的起止位置与字面正确", () -> {
            Lattice lattice = Lattice.build("研究生", dict);
            List<String> edges = new ArrayList<>();
            for (int end = 1; end <= 3; end++) {
                for (Lattice.Edge e : lattice.incomingAt(end)) {
                    edges.add(e.start() + "-" + e.end() + ":" + e.surface()
                            + (e.known() ? "" : "(未知)"));
                    checkEq(e.surface(), "研究生".substring(e.start(), e.end()), "字面对齐");
                }
            }
            // 至少包含：0-2研究、0-3研究生、2-3生、1-2究(未知)、0-1研(未知)
            check(edges.contains("0-2:研究"), "含 研究 边: " + edges);
            check(edges.contains("0-3:研究生"), "含 研究生 边: " + edges);
            check(edges.contains("2-3:生"), "含 生 边: " + edges);
            check(edges.stream().anyMatch(s -> s.equals("1-2:究(未知)")), "含未知 究: " + edges);
        });
    }
}
