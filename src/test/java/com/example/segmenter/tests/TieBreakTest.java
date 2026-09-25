package com.example.segmenter.tests;

import com.example.segmenter.api.Segmenter;
import com.example.segmenter.model.Dictionary;
import com.example.segmenter.model.Segmentation;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import static com.example.segmenter.tests.TestFramework.check;
import static com.example.segmenter.tests.TestFramework.assertEquals;
import static com.example.segmenter.tests.TestFramework.suite;

/**
 * 同分确定性决胜测试：用显式代价手工构造“切分歧义但总代价严格相同”的句子，
 * 验证决胜规则（代价 → 词数 → 码点字典序 → 未知标记）与重复运行/词典插入顺序无关。
 */
public final class TieBreakTest {

    private TieBreakTest() {
    }

    private static Dictionary costs(String version, String[][] entries) {
        Map<String, Double> map = new LinkedHashMap<>();
        for (String[] e : entries) {
            map.put(e[0], Double.parseDouble(e[1]));
        }
        return Dictionary.fromCosts(version, map);
    }

    public static void run() {
        suite("deterministic tie-breaking");

        // 情形 1：同分，词数不同 → 词数少者胜（倾向长词）
        // 句子 "AB"：A+B 两词各 1.0（合计 2.0）；AB 一词 2.0。
        Dictionary d1 = costs("tie-count", new String[][]{
                {"A", "1.0"}, {"B", "1.0"}, {"AB", "2.0"}
        });
        Segmenter seg1 = new Segmenter(d1, 10.0);
        assertEquals("同分时词少者胜", List.of("AB"), seg1.segment("AB").words());

        // 情形 2：同分且同词数 → 按码点字典序，靠左差异定序
        // 句子 "XY"：X+Y 与 P+Y 不可能同时覆盖同一文本……改为同文本的两组切分：
        // "MN"：M=2.0,N=1.0（合计3.0）；MN=3.0 会走词少，故这里让两条路径词数相同：
        // 句子 "XYZ"：XY+Z  与  X+YZ，合计都为 3.0。
        // 词序列 ["XY","Z"] vs ["X","YZ"]：第一词 "X" 是 "XY" 前缀，"X" 更短 → X+YZ 胜。
        Dictionary d2 = costs("tie-lex", new String[][]{
                {"X", "1.0"}, {"Y", "10.0"}, {"Z", "2.0"},
                {"XY", "2.0"}, {"YZ", "1.0"}
        });
        // XY(2.0)+Z(2.0)=4.0；X(1.0)+YZ(1.0)=2.0 —— 不同价，需调成同价：
        Dictionary d2b = costs("tie-lex2", new String[][]{
                {"X", "2.0"}, {"YZ", "2.0"}, {"XY", "2.0"}, {"Z", "2.0"}
        });
        Segmenter seg2 = new Segmenter(d2b, 10.0);
        // ["X","YZ"] vs ["XY","Z"]：第一词 "X" < "XY"（前缀更短）→ X/YZ 胜
        assertEquals("同词数按字典序（前缀短者胜）",
                List.of("X", "YZ"), seg2.segment("XYZ").words());

        // 情形 3：同分同词数，第一词不同字符 → 码点小者胜
        // 句子 "AB" 只有一条 2 词切分；用三字段制造两条等词数路径：
        // "ABC"：AB+C 与 A+BC。令 AB=1,C=2,A=1,BC=2，合计都为 3.0。
        // 第一词 "A" vs "AB"："A" 是前缀 → A/BC 胜（与情形 2 同族）。
        // 再构造第一词等长不同字：句子 "ABCD"：
        //   AB+CD = 1.5+1.5 = 3.0；AC+BD 不能覆盖同文本，故用重叠位置的词：
        //   "ABC"：AB+C（AB=1.5,C=1.5）与 A+BC（A=1.5,BC=1.5）——同上。
        // 为得到“等长第一词不同字”，需要两条路径的第一词终点相同但词面不同，
        // 这在确定文本上不可能（同子串只有一个词面）。因此字典序的首次差异
        // 只能是前缀关系；上面两例已覆盖。这里验证非前缀的码点差异：
        // 句子 "ABCD"：AB+CD（1.0+2.0=3.0） vs A+BC+D（1.0+1.0+1.0=3.0，三词）→ 词少者胜。
        Dictionary d3 = costs("tie-mixed", new String[][]{
                {"AB", "1.0"}, {"CD", "2.0"},
                {"A", "1.0"}, {"BC", "1.0"}, {"D", "1.0"}
        });
        Segmenter seg3 = new Segmenter(d3, 10.0);
        assertEquals("代价相同词数不同优先词少",
                List.of("AB", "CD"), seg3.segment("ABCD").words());

        // 情形 4：词面完全相同的两条候选（词典单字词 vs 未知单字），
        // 未知代价被设成与词典代价相同 → 词典边(false)按最后决胜规则排在前面。
        Dictionary d4 = costs("tie-unknown", new String[][]{
                {"Q", "5.0"}
        });
        Segmenter seg4 = new Segmenter(d4, 5.0);
        Segmentation s4 = seg4.segment("Q");
        assertEquals("词面相同时词典词优先", List.of("Q"), s4.words());
        check("胜出路径不标未知", !s4.tokens().get(0).unknown());
        // N=2 时第二条应是未知字版本
        List<Segmentation> nb4 = seg4.segmentNBest("Q", 2);
        assertEquals("词面相同候选共有 2 条", 2, nb4.size());
        check("第 2 条标记为未知", nb4.get(1).tokens().get(0).unknown());
        check("两条总代价相同",
                Math.abs(nb4.get(0).totalCost() - nb4.get(1).totalCost()) < 1e-12);

        // 情形 5：决定性与词典插入顺序无关 —— 同样的词与代价，调换插入顺序。
        Map<String, Double> orderA = new LinkedHashMap<>();
        orderA.put("AB", 2.0);
        orderA.put("A", 1.0);
        orderA.put("BC", 2.0);
        orderA.put("B", 10.0);
        orderA.put("C", 10.0);
        Map<String, Double> orderB = new LinkedHashMap<>();
        orderB.put("C", 10.0);
        orderB.put("B", 10.0);
        orderB.put("BC", 2.0);
        orderB.put("A", 1.0);
        orderB.put("AB", 2.0);
        Segmenter sa = new Segmenter(Dictionary.fromCosts("oa", orderA), 10.0);
        Segmenter sb = new Segmenter(Dictionary.fromCosts("ob", orderB), 10.0);
        // ABC: AB(2)+C(10)=12；A(1)+BC(2)=3 → A/BC 胜。两实现应一致。
        List<String> wa = sa.segment("ABC").words();
        List<String> wb = sb.segment("ABC").words();
        assertEquals("插入顺序不影响结果 A", List.of("A", "BC"), wa);
        assertEquals("插入顺序不影响结果 B", List.of("A", "BC"), wb);

        // 情形 6：重复运行结果稳定（跑 50 次）
        boolean stable = true;
        List<String> first = sa.segmentNBest("ABC", 5).get(0).words();
        for (int i = 0; i < 50; i++) {
            if (!sa.segmentNBest("ABC", 5).get(0).words().equals(first)) {
                stable = false;
                break;
            }
        }
        check("重复运行 50 次结果稳定", stable);
    }
}
