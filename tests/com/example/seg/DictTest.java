package com.example.seg;

import com.example.seg.dict.Dictionary;
import com.example.seg.dict.DictionaryRegistry;
import com.example.seg.model.Costs;
import com.example.seg.model.SegPath;
import com.example.seg.seg.DpSegmenter;

import java.io.IOException;
import java.io.StringReader;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;

/**
 * 词典相关测试：文件解析、指令识别、注释容错、目录加载与"字典版本切换"。
 */
public final class DictTest extends TestCase {

    public static void main(String[] args) throws Exception {
        System.exit(TestCase.run(new DictTest()));
    }

    private final DpSegmenter dp = new DpSegmenter();

    @Override
    protected void run() {
        testParseWithComments();
        testUnknownCostDirective();
        testMissingDirective();
        testBadEntry();
        testNegativeCostRejected();
        testProgrammaticDictionary();
        testVersionSwitch();
        testLoadDirectoryFromData();
        testDescriptionDirectiveLineIsComment();
    }

    private void testParseWithComments() {
        test("解析：注释/空行被忽略，词行生效", () -> {
            String text = """
                    # 头部注释
                    # @unknown-cost 5
                    # @description 测试词典
                    研究 1
                      # 缩进注释
                    研究生 2.5
                    """;
            Dictionary d = Dictionary.parse("t", new StringReader(text));
            checkEq(d.size(), 2, "两个词");
            checkEq(d.unknownCostScaled(), Costs.ofInt(5), "未知代价 5");
            checkEq(d.description(), "测试词典", "描述");
            checkEq(d.declaredCost("研究"), Costs.toBigDecimal(Costs.ofInt(1)), "研究代价");
        });
    }

    private void testUnknownCostDirective() {
        test("解析：@unknown-cost 说明文字行不误判，真正指令生效", () -> {
            String text = """
                    # @unknown-cost 未登录单字的统一代价（这一行是说明）
                    # @unknown-cost 7
                    甲 1
                    """;
            Dictionary d = Dictionary.parse("t", new StringReader(text));
            checkEq(d.unknownCostScaled(), Costs.ofInt(7), "应以数值指令 7 为准");
        });
    }

    private void testDescriptionDirectiveLineIsComment() {
        test("解析：@description 后无值时描述为空且不报错", () -> {
            String text = "# @unknown-cost 3\n甲\t1\n";
            Dictionary d = Dictionary.parse("t", new StringReader(text));
            checkEq(d.description(), "", "空描述");
            checkEq(d.size(), 1, "一个词（支持 tab 分隔）");
        });
    }

    private void testMissingDirective() {
        test("解析：缺少 @unknown-cost 报错", () -> {
            IOException caught = null;
            try {
                Dictionary.parse("t", new StringReader("甲 1\n"));
            } catch (IOException e) {
                caught = e;
            }
            check(caught != null && caught.getMessage().contains("@unknown-cost"),
                    "应报缺少指令: " + (caught == null ? "无异常" : caught.getMessage()));
        });
    }

    private void testBadEntry() {
        test("解析：畸形词行报错", () -> {
            IOException caught = null;
            try {
                Dictionary.parse("t", new StringReader("# @unknown-cost 1\n孤零零\n"));
            } catch (IOException e) {
                caught = e;
            }
            check(caught != null, "应解析失败");
        });
    }

    private void testNegativeCostRejected() {
        test("解析：负代价被拒绝", () -> {
            Exception caught = null;
            try {
                Dictionary.parse("t", new StringReader("# @unknown-cost -1\n甲 1\n"));
            } catch (Exception e) {
                caught = e;
            }
            check(caught instanceof IOException, "负的 unknown-cost 应报 IOException");

            caught = null;
            try {
                Dictionary.builder("x", 1).add("甲", "-2");
            } catch (Exception e) {
                caught = e;
            }
            check(caught instanceof IllegalArgumentException, "负词代价应抛异常");
        });
    }

    private void testProgrammaticDictionary() {
        test("编程式词典：构造与查询", () -> {
            Dictionary d = Dictionary.builder("p", Costs.ofInt(9))
                    .add("甲乙", "2.5").build();
            checkEq(d.maxWordLength(), 2, "最大词长");
            check(d.trie().contains("甲乙"), "包含词");
            check(!d.trie().contains("甲"), "不包含未加的词");
        });
    }

    private void testVersionSwitch() {
        test("版本切换：同句在两个版本下最优不同", () -> {
            // vA：研究便宜 -> 研究/生/生命 最优
            Dictionary a = Dictionary.builder("A", Costs.ofInt(5))
                    .add("研究", "1").add("研究生", "4").add("生命", "1")
                    .add("生", "1").add("命", "4").build();
            // vB：研究生便宜 + 生命贵 -> 研究生/生命 最优
            Dictionary b = Dictionary.builder("B", Costs.ofInt(8))
                    .add("研究", "4").add("研究生", "2.5").add("生命", "4")
                    .add("生", "2").add("命", "3").build();

            String text = "研究生生命";
            SegPath pa = dp.segment(text, a);
            SegPath pb = dp.segment(text, b);
            checkEq(pa.joined(), "研究/生/生命", "版本A最优");
            checkEq(pb.joined(), "研究生/生命", "版本B最优");
            check(!pa.joined().equals(pb.joined()), "两版本结果必须不同");

            DictionaryRegistry reg = new DictionaryRegistry();
            reg.register(a);
            reg.register(b);
            checkEq(List.copyOf(reg.versions()), List.of("A", "B"), "注册表保留版本顺序");
            check(reg.get("A") == a && reg.get("B") == b, "切换只是换引用");
        });
    }

    private void testLoadDirectoryFromData() {
        test("目录加载：data/dicts 下 v1、v2 均可加载", () -> {
            Path dir = Path.of("data/dicts");
            if (!Files.isDirectory(dir)) {
                throw new IllegalStateException("需要在项目根目录下运行测试，当前缺少 "
                        + dir.toAbsolutePath());
            }
            DictionaryRegistry reg = DictionaryRegistry.loadDirectory(dir);
            check(reg.versions().contains("v1"), "有 v1");
            check(reg.versions().contains("v2"), "有 v2");
            checkEq(reg.get("v1").size(), 16, "v1 词数");
            checkEq(reg.get("v2").size(), 16, "v2 词数");
        });
    }
}
