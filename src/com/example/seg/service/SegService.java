package com.example.seg.service;

import com.example.seg.dict.Dictionary;
import com.example.seg.dict.DictionaryRegistry;
import com.example.seg.model.Costs;
import com.example.seg.model.SegPath;
import com.example.seg.model.Token;
import com.example.seg.seg.DpSegmenter;
import com.example.seg.seg.ExhaustiveSegmenter;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 纯业务层：拿到 DictionaryRegistry 后完成分词、N 最佳、穷举对照，
 * 不接触 HTTP，方便直接单元测试。
 *
 * 所有返回值都是 Map/List/String/Number，可直接交给 {@link Json} 序列化。
 */
public final class SegService {

    public static final int MAX_TEXT_CHARS = 10_000;
    public static final int MAX_K = 64;
    public static final int DEFAULT_K = 10;

    private final DictionaryRegistry registry;
    private final DpSegmenter dp = new DpSegmenter();
    private final ExhaustiveSegmenter exhaustive = new ExhaustiveSegmenter();

    public SegService(DictionaryRegistry registry) {
        this.registry = registry;
    }

    public DictionaryRegistry registry() {
        return registry;
    }

    /** 成功 / 失败结果。失败时 httpStatus 为 4xx，errorCode 供程序判断。 */
    public static final class Result {
        public final int httpStatus;
        public final String errorCode;
        public final Map<String, Object> body;

        private Result(int httpStatus, String errorCode, Map<String, Object> body) {
            this.httpStatus = httpStatus;
            this.errorCode = errorCode;
            this.body = body;
        }

        static Result ok(Map<String, Object> body) {
            return new Result(200, null, body);
        }

        static Result error(int httpStatus, String errorCode, String message) {
            Map<String, Object> body = new LinkedHashMap<>();
            body.put("error", errorCode);
            body.put("message", message);
            return new Result(httpStatus, errorCode, body);
        }
    }

    // ------------------------------------------------------------------
    // 元信息
    // ------------------------------------------------------------------

    public Result listDictionaries() {
        List<Object> dicts = new ArrayList<>();
        for (String version : registry.versions()) {
            Dictionary d = registry.get(version);
            Map<String, Object> info = new LinkedHashMap<>();
            info.put("version", d.version());
            info.put("description", d.description());
            info.put("entryCount", d.size());
            info.put("maxWordLength", d.maxWordLength());
            info.put("unknownCharCost", Costs.toBigDecimal(d.unknownCostScaled()));
            dicts.add(info);
        }
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("dictionaries", dicts);
        return Result.ok(body);
    }

    // ------------------------------------------------------------------
    // 分词（best 或 nbest）
    // ------------------------------------------------------------------

    public Result segment(String version, String text, Integer k) {
        Dictionary dict = registry.get(version);
        if (dict == null) {
            return Result.error(404, "DICT_NOT_FOUND",
                    "词典版本不存在: " + version + "，可用版本: " + registry.versions());
        }
        if (text == null) {
            return Result.error(400, "MISSING_TEXT", "缺少 text 字段");
        }
        if (text.length() > MAX_TEXT_CHARS) {
            return Result.error(400, "TEXT_TOO_LONG",
                    "text 最长 " + MAX_TEXT_CHARS + " 字符，实际 " + text.length());
        }
        int kk = k == null ? 1 : k;
        if (kk < 1 || kk > MAX_K) {
            return Result.error(400, "BAD_K", "k 必须在 1.." + MAX_K + " 之间，实际 " + kk);
        }

        List<SegPath> paths = dp.nbest(text, dict, kk);
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("text", text);
        body.put("dictionary", version);
        body.put("requestedK", kk);
        body.put("returnedK", paths.size());
        body.put("best", renderPath(paths.get(0)));
        if (kk > 1) {
            body.put("nbest", renderPaths(paths));
        }
        return Result.ok(body);
    }

    // ------------------------------------------------------------------
    // 穷举对照
    // ------------------------------------------------------------------

    public Result crosscheck(String version, String text, Integer k) {
        Dictionary dict = registry.get(version);
        if (dict == null) {
            return Result.error(404, "DICT_NOT_FOUND",
                    "词典版本不存在: " + version + "，可用版本: " + registry.versions());
        }
        if (text == null) {
            return Result.error(400, "MISSING_TEXT", "缺少 text 字段");
        }
        if (text.length() > ExhaustiveSegmenter.MAX_EXHAUSTIVE_CHARS) {
            return Result.error(400, "TEXT_TOO_LONG",
                    "穷举对照只支持不超过 " + ExhaustiveSegmenter.MAX_EXHAUSTIVE_CHARS
                            + " 个字符的句子，实际 " + text.length());
        }
        int kk = k == null ? DEFAULT_K : k;
        if (kk < 1 || kk > MAX_K) {
            return Result.error(400, "BAD_K", "k 必须在 1.." + MAX_K + " 之间，实际 " + kk);
        }

        List<SegPath> all = exhaustive.enumerate(text, dict);
        List<SegPath> dpPaths = dp.nbest(text, dict, kk);

        int compareCount = Math.min(kk, all.size());
        List<String> mismatches = new ArrayList<>();
        for (int i = 0; i < compareCount; i++) {
            SegPath a = all.get(i);
            SegPath b = i < dpPaths.size() ? dpPaths.get(i) : null;
            if (b == null || SegPath.tieCompare(a, b) != 0 || !sameTokens(a, b)) {
                mismatches.add("第 " + (i + 1) + " 条不一致：穷举=" + pathSignature(a)
                        + "，DP=" + (b == null ? "<缺失>" : pathSignature(b)));
            }
        }

        Map<String, Object> body = new LinkedHashMap<>();
        body.put("text", text);
        body.put("dictionary", version);
        body.put("totalSegmentations", all.size());
        body.put("comparedK", compareCount);
        body.put("match", mismatches.isEmpty());
        body.put("mismatches", mismatches);
        body.put("exhaustiveTop", renderPaths(all.subList(0, compareCount)));
        body.put("dpTop", renderPaths(dpPaths));
        return Result.ok(body);
    }

    // ------------------------------------------------------------------
    // 渲染
    // ------------------------------------------------------------------

    private List<Object> renderPaths(List<SegPath> paths) {
        List<Object> list = new ArrayList<>(paths.size());
        for (int i = 0; i < paths.size(); i++) {
            Map<String, Object> rendered = renderPath(paths.get(i));
            rendered.put("rank", i + 1);
            list.add(rendered);
        }
        return list;
    }

    private Map<String, Object> renderPath(SegPath path) {
        List<Object> tokens = new ArrayList<>(path.tokens().size());
        for (Token t : path.tokens()) {
            Map<String, Object> tj = new LinkedHashMap<>();
            tj.put("surface", t.surface());
            tj.put("known", t.known());
            tj.put("cost", Costs.toBigDecimal(t.cost()));
            tj.put("dictCost", t.dictCost());
            tokens.add(tj);
        }
        Map<String, Object> p = new LinkedHashMap<>();
        p.put("tokens", tokens);
        p.put("segmentation", path.joined());
        p.put("totalCost", Costs.toBigDecimal(path.cost()));
        return p;
    }

    private static boolean sameTokens(SegPath a, SegPath b) {
        if (a.tokens().size() != b.tokens().size()) {
            return false;
        }
        for (int i = 0; i < a.tokens().size(); i++) {
            Token x = a.tokens().get(i);
            Token y = b.tokens().get(i);
            if (!x.surface().equals(y.surface()) || x.known() != y.known()
                    || x.cost() != y.cost()) {
                return false;
            }
        }
        return true;
    }

    private static String pathSignature(SegPath p) {
        return p.joined() + "(cost=" + Costs.toString(p.cost()) + ",n=" + p.tokens().size() + ")";
    }
}
