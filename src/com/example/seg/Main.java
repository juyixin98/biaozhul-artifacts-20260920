package com.example.seg;

import com.example.seg.dict.Dictionary;
import com.example.seg.dict.DictionaryRegistry;
import com.example.seg.model.Costs;
import com.example.seg.model.SegPath;
import com.example.seg.seg.DpSegmenter;
import com.example.seg.seg.ExhaustiveSegmenter;

import java.nio.file.Paths;
import java.util.List;

/**
 * 命令行入口（无需 HTTP 即可本地验证）。
 *
 * 用法：
 *   java com.example.seg.Main demo [dictDir]
 *       对内置合成例句演示：最小代价分词、N 最佳、穷举对照、词典版本切换
 *   java com.example.seg.Main seg  <version> <text> [k] [dictDir]
 *       打印 1 条或至多 k 条最佳切分
 *   java com.example.seg.Main enum <version> <text> [dictDir]
 *       穷举所有切分（限 16 字），与 DP 的前若干条对照打印
 * 无参数等价于 demo。
 */
public final class Main {

    private static final String[] DEMO_SENTENCES = {
            "研究生生命",       // 经典交叉歧义：研究/生/命 vs 研究生/命
            "结婚的和尚未结婚的", // 重叠词：结婚 / 尚未 / 未婚
            "研究生命起源X",    // 含未知字符 X（词典未收录）
            "",                // 空串
    };

    public static void main(String[] args) throws Exception {
        String command = args.length == 0 ? "demo" : args[0];
        switch (command) {
            case "demo" -> runDemo(args.length >= 2 ? args[1] : "data/dicts");
            case "seg" -> runSeg(args);
            case "enum" -> runEnum(args);
            default -> {
                System.err.println("未知命令: " + command);
                System.exit(2);
            }
        }
    }

    private static DictionaryRegistry registry(String dir) throws Exception {
        return DictionaryRegistry.loadDirectory(Paths.get(dir));
    }

    private static void runDemo(String dictDir) throws Exception {
        DictionaryRegistry reg = registry(dictDir);
        DpSegmenter dp = new DpSegmenter();
        ExhaustiveSegmenter ex = new ExhaustiveSegmenter();

        System.out.println("=== 词典版本 ===");
        for (String v : reg.versions()) {
            Dictionary d = reg.get(v);
            System.out.printf("  %s: %d 词, 最大词长 %d, 未知字代价 %s  (%s)%n",
                    d.version(), d.size(), d.maxWordLength(),
                    Costs.toString(d.unknownCostScaled()), d.description());
        }

        for (String version : reg.versions()) {
            Dictionary dict = reg.get(version);
            System.out.println();
            System.out.println("=== 词典版本 " + version + " ===");
            for (String text : DEMO_SENTENCES) {
                String shown = text.isEmpty() ? "<空串>" : text;
                System.out.println("-- 输入: " + shown);
                if (text.isEmpty()) {
                    SegPath best = dp.segment(text, dict);
                    System.out.println("   最佳: <空>  总代价=" + Costs.toString(best.cost()));
                    continue;
                }
                SegPath best = dp.segment(text, dict);
                System.out.println("   最佳: " + best.joined()
                        + "  总代价=" + Costs.toString(best.cost()));
                List<SegPath> nbest = dp.nbest(text, dict, 5);
                for (int i = 0; i < nbest.size(); i++) {
                    SegPath p = nbest.get(i);
                    System.out.printf("   #%d %-20s 总代价=%s token数=%d%n",
                            i + 1, p.joined(), Costs.toString(p.cost()), p.tokens().size());
                }
                if (text.length() <= ExhaustiveSegmenter.MAX_EXHAUSTIVE_CHARS) {
                    List<SegPath> all = ex.enumerate(text, dict);
                    boolean agree = all.get(0).joined().equals(best.joined())
                            && all.get(0).cost() == best.cost();
                    System.out.println("   穷举路径总数=" + all.size()
                            + "，穷举最优=" + all.get(0).joined()
                            + "，与DP一致=" + agree);
                }
            }
        }

        // 显式演示版本切换：同一句话在两个版本下最优不同
        String probe = "研究生生命";
        if (reg.get("v1") != null && reg.get("v2") != null) {
            System.out.println();
            System.out.println("=== 版本切换对照（同一句: " + probe + "）===");
            for (String v : new String[]{"v1", "v2"}) {
                SegPath p = dp.segment(probe, reg.get(v));
                System.out.println("  版本 " + v + " -> " + p.joined()
                        + "  总代价=" + Costs.toString(p.cost()));
            }
        }
    }

    private static void runSeg(String[] args) throws Exception {
        if (args.length < 3) {
            usage("seg");
        }
        String version = args[1];
        String text = args[2];
        int k = args.length >= 4 ? Integer.parseInt(args[3]) : 1;
        String dir = args.length >= 5 ? args[4] : "data/dicts";
        Dictionary dict = registry(dir).get(version);
        if (dict == null) {
            System.err.println("词典版本不存在: " + version);
            System.exit(1);
        }
        List<SegPath> paths = new DpSegmenter().nbest(text, dict, k);
        for (int i = 0; i < paths.size(); i++) {
            SegPath p = paths.get(i);
            System.out.printf("#%d %s  总代价=%s  token数=%d%n",
                    i + 1, p.joined(), Costs.toString(p.cost()), p.tokens().size());
        }
    }

    private static void runEnum(String[] args) throws Exception {
        if (args.length < 3) {
            usage("enum");
        }
        String version = args[1];
        String text = args[2];
        String dir = args.length >= 4 ? args[3] : "data/dicts";
        Dictionary dict = registry(dir).get(version);
        if (dict == null) {
            System.err.println("词典版本不存在: " + version);
            System.exit(1);
        }
        List<SegPath> all = new ExhaustiveSegmenter().enumerate(text, dict);
        System.out.println("穷举路径总数: " + all.size());
        for (int i = 0; i < all.size(); i++) {
            SegPath p = all.get(i);
            System.out.printf("#%d %s  总代价=%s  token数=%d%n",
                    i + 1, p.joined(), Costs.toString(p.cost()), p.tokens().size());
        }
    }

    private static void usage(String cmd) {
        System.err.println("参数不足: java com.example.seg.Main " + cmd
                + (cmd.equals("seg") ? " <version> <text> [k] [dictDir]"
                : " <version> <text> [dictDir]"));
        System.exit(2);
    }
}
