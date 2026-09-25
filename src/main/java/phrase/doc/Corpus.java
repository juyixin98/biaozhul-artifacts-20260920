package phrase.doc;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 自建合成语料（纯本地、硬编码，无外部文档源/无大模型）。
 *
 * <p>构造意图（与测试一一对应）：
 * <ul>
 *   <li>d1 重复词 "echo" 在同字段出现 4 次，标题+正文跨字段也可组成 echo echo；</li>
 *   <li>d2 跨字段短语："data pipeline" 跨越 body→tags（默认 fieldGap=0 时 slop=0 命中）；</li>
 *   <li>d3 重复词 "rain" 三次（标题 1 次、正文 2 次），与 d1 区分；</li>
 *   <li>d4 经典“贪心算法陷阱”序列 alpha alpha beta（重复 alpha）；</li>
 *   <li>d5 含停用词 the/in，专门验证“停用词保留原始位置”；</li>
 *   <li>d6 同字段 a b a（回指型重复词），用于穷举枚举参考；</li>
 *   <li>d7 跨字段且带 gap 的边界验证："blue ocean" 跨 title→body，中间恰好 1 个词。</li>
 * </ul>
 */
public final class Corpus {

    private Corpus() {}

    public static List<Doc> synthetic() {
        return List.of(
                doc1(),
                doc2(),
                doc3(),
                doc4(),
                doc5(),
                doc6(),
                doc7());
    }

    /** d1：echo 在 body 出现 4 次（位置 2..5），title 1 次。 */
    static Doc doc1() {
        Map<String, String> f = new LinkedHashMap<>();
        f.put("title", "echo chamber");
        f.put("body", "we echo echo echo echo today");
        return new Doc("d1", f);
    }

    /** d2：data pipeline 跨越 body("...pipeline data") 与 tags("pipeline")。 */
    static Doc doc2() {
        Map<String, String> f = new LinkedHashMap<>();
        f.put("title", "infra notes");
        f.put("body", "we build a pipeline for data");
        f.put("tags", "pipeline streaming");
        return new Doc("d2", f);
    }

    /** d3：rain 重复（title 1 次，body 2 次）。 */
    static Doc doc3() {
        Map<String, String> f = new LinkedHashMap<>();
        f.put("title", "rain watch");
        f.put("body", "the rain brings more rain");
        return new Doc("d3", f);
    }

    /** d4：经典贪心陷阱序列 alpha alpha beta。 */
    static Doc doc4() {
        Map<String, String> f = new LinkedHashMap<>();
        f.put("title", "pattern");
        f.put("body", "alpha alpha beta");
        return new Doc("d4", f);
    }

    /** d5：含停用词；"cat sat mat" 靠 slop 跳过 the/in。 */
    static Doc doc5() {
        Map<String, String> f = new LinkedHashMap<>();
        f.put("title", "the cat");
        f.put("body", "the cat sat in the mat");
        return new Doc("d5", f);
    }

    /** d6：回指型重复词 a b a，用于穷举枚举参考。 */
    static Doc doc6() {
        Map<String, String> f = new LinkedHashMap<>();
        f.put("title", "aba");
        f.put("body", "a b a");
        return new Doc("d6", f);
    }

    /** d7：跨字段 gap 边界：blue 是 title 末词，ocean 是 body 首词。 */
    static Doc doc7() {
        Map<String, String> f = new LinkedHashMap<>();
        f.put("title", "deep blue");
        f.put("body", "ocean currents");
        return new Doc("d7", f);
    }
}
