package com.example.seg;

import com.example.seg.dict.Dictionary;
import com.example.seg.dict.DictionaryRegistry;
import com.example.seg.model.Costs;
import com.example.seg.service.Json;
import com.example.seg.service.SegService;

import java.math.BigDecimal;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 服务层与 JSON 测试：参数校验、404/400 分支、N 最佳响应结构、穷举对照 match 标志。
 */
public final class ServiceTest extends TestCase {

    public static void main(String[] args) throws Exception {
        System.exit(TestCase.run(new ServiceTest()));
    }

    private SegService service;

    private final Dictionary v1 = Dictionary.builder("v1", Costs.ofInt(5))
            .description("版本一")
            .add("研究", "1").add("研究生", "4").add("生命", "1")
            .add("生", "1").add("命", "4")
            .add("结婚", "2").add("尚未", "1.5").add("的", "1").add("和", "1")
            .build();
    private final Dictionary v2 = Dictionary.builder("v2", Costs.ofInt(8))
            .add("研究", "4").add("研究生", "2.5").add("生命", "4")
            .add("生", "2").add("命", "3").build();

    @Override
    protected void run() {
        RegistrySetup();
        testListDictionaries();
        testSegmentBest();
        testSegmentNbest();
        testSegmentEmpty();
        testSegmentUnknown();
        testErrors();
        testCrosscheck();
        testJsonRoundTrip();
        testJsonChineseAndEscapes();
        testJsonInvalid();
        testResponseSerializes();
    }

    private void RegistrySetup() {
        test("准备：注册两个词典版本", () -> {
            DictionaryRegistry reg = new DictionaryRegistry();
            reg.register(v1);
            reg.register(v2);
            service = new SegService(reg);
            check(service != null, "service 构造");
        });
    }

    private void testListDictionaries() {
        test("GET dictionaries：返回两个版本及元信息", () -> {
            SegService.Result r = service.listDictionaries();
            checkEq(r.httpStatus, 200, "200");
            @SuppressWarnings("unchecked")
            List<Map<String, Object>> dicts = (List<Map<String, Object>>) r.body.get("dictionaries");
            checkEq(dicts.size(), 2, "两个版本");
            checkEq(dicts.get(0).get("version"), "v1", "顺序 v1");
            checkEq(dicts.get(0).get("unknownCharCost"), new BigDecimal("5"), "v1 未知代价");
            checkEq(dicts.get(1).get("unknownCharCost"), new BigDecimal("8"), "v2 未知代价");
        });
    }

    @SuppressWarnings("unchecked")
    private Map<String, Object> best(SegService.Result r) {
        return (Map<String, Object>) r.body.get("best");
    }

    @SuppressWarnings("unchecked")
    private List<Map<String, Object>> nbest(SegService.Result r) {
        return (List<Map<String, Object>>) r.body.get("nbest");
    }

    private void testSegmentBest() {
        test("segment：v1 最优切分", () -> {
            SegService.Result r = service.segment("v1", "研究生生命", null);
            checkEq(r.httpStatus, 200, "200");
            checkEq(best(r).get("segmentation"), "研究/生/生命", "切分");
            checkEq(best(r).get("totalCost"), new BigDecimal("3"), "代价");
            checkEq(r.body.get("returnedK"), 1, "默认只返回 1 条");
            check(!r.body.containsKey("nbest"), "k=1 时不附 nbest 数组");
        });
    }

    private void testSegmentNbest() {
        test("segment：k=3 返回排序的前三条", () -> {
            SegService.Result r = service.segment("v1", "研究生生命", 3);
            checkEq(r.httpStatus, 200, "200");
            checkEq(r.body.get("returnedK"), 3, "3 条");
            List<Map<String, Object>> nb = nbest(r);
            checkEq(nb.get(0).get("segmentation"), "研究/生/生命", "第1");
            checkEq(nb.get(0).get("rank"), 1, "rank 从1开始");
            checkEq(nb.get(1).get("rank"), 2, "rank 2");
            // 代价单调不降
            BigDecimal c0 = (BigDecimal) nb.get(0).get("totalCost");
            BigDecimal c1 = (BigDecimal) nb.get(1).get("totalCost");
            BigDecimal c2 = (BigDecimal) nb.get(2).get("totalCost");
            check(c0.compareTo(c1) <= 0 && c1.compareTo(c2) <= 0, "代价单调");
        });
    }

    private void testSegmentEmpty() {
        test("segment：空串返回空 tokens 与 0 代价", () -> {
            SegService.Result r = service.segment("v1", "", null);
            checkEq(r.httpStatus, 200, "200");
            checkEq(best(r).get("segmentation"), "", "空切分");
            checkEq(best(r).get("totalCost"), new BigDecimal("0"), "0 代价");
            @SuppressWarnings("unchecked")
            List<?> tokens = (List<?>) best(r).get("tokens");
            checkEq(tokens.size(), 0, "无 token");
        });
    }

    private void testSegmentUnknown() {
        test("segment：未知字在 token 里 known=false 且无 dictCost", () -> {
            SegService.Result r = service.segment("v1", "X", null);
            @SuppressWarnings("unchecked")
            Map<String, Object> token =
                    ((List<Map<String, Object>>) best(r).get("tokens")).get(0);
            checkEq(token.get("surface"), "X", "字面");
            checkEq(token.get("known"), Boolean.FALSE, "未知标记");
            checkEq(token.get("cost"), new BigDecimal("5"), "按未知代价");
            check(token.get("dictCost") == null, "dictCost 为 null");
        });
    }

    private void testErrors() {
        test("错误：版本不存在 404", () -> {
            SegService.Result r = service.segment("nope", "命", null);
            checkEq(r.httpStatus, 404, "404");
            checkEq(r.body.get("error"), "DICT_NOT_FOUND", "错误码");
        });
        test("错误：text 缺失 400", () -> {
            SegService.Result r = service.segment("v1", null, null);
            checkEq(r.httpStatus, 400, "400");
            checkEq(r.body.get("error"), "MISSING_TEXT", "错误码");
        });
        test("错误：text 超长 400", () -> {
            String longText = "命".repeat(SegService.MAX_TEXT_CHARS + 1);
            SegService.Result r = service.segment("v1", longText, null);
            checkEq(r.httpStatus, 400, "400");
            checkEq(r.body.get("error"), "TEXT_TOO_LONG", "错误码");
        });
        test("错误：k 越界 400", () -> {
            checkEq(service.segment("v1", "命", 0).httpStatus, 400, "k=0");
            checkEq(service.segment("v1", "命", SegService.MAX_K + 1).httpStatus, 400, "k过大");
        });
        test("错误：crosscheck 长句 400", () -> {
            String s = "命".repeat(17);
            SegService.Result r = service.crosscheck("v1", s, 5);
            checkEq(r.httpStatus, 400, "400");
            checkEq(r.body.get("error"), "TEXT_TOO_LONG", "错误码");
        });
    }

    private void testCrosscheck() {
        test("crosscheck：小句上 match=true 且穷举/DP 条数一致", () -> {
            SegService.Result r = service.crosscheck("v1", "结婚的和尚未", 10);
            checkEq(r.httpStatus, 200, "200");
            checkEq(r.body.get("match"), Boolean.TRUE, "应一致");
            @SuppressWarnings("unchecked")
            List<?> mismatches = (List<?>) r.body.get("mismatches");
            checkEq(mismatches.size(), 0, "无不一致");
            check(((Number) r.body.get("totalSegmentations")).intValue() >= 1, "路径总数 >=1");
        });

        test("crosscheck：空串 match=true", () -> {
            SegService.Result r = service.crosscheck("v1", "", 10);
            checkEq(r.body.get("match"), Boolean.TRUE, "空串一致");
            checkEq(r.body.get("totalSegmentations"), 1, "空串恰一条路径");
        });
    }

    private void testJsonRoundTrip() {
        test("JSON：对象/数组/数字/中文往返", () -> {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("version", "v1");
            m.put("cost", new BigDecimal("3.5"));
            m.put("ok", Boolean.TRUE);
            m.put("nothing", null);
            m.put("items", List.of("研究", "生命"));
            String json = Json.write(m);
            @SuppressWarnings("unchecked")
            Map<String, Object> back = (Map<String, Object>) Json.parse(json);
            checkEq(back.get("version"), "v1", "字符串");
            checkEq(back.get("cost"), new BigDecimal("3.5"), "数字精度");
            checkEq(back.get("ok"), Boolean.TRUE, "布尔");
            check(back.get("nothing") == null, "null");
            checkEq(back.get("items"), List.of("研究", "生命"), "数组与中文");
        });
    }

    private void testJsonChineseAndEscapes() {
        test("JSON：转义字符与中文直接输出", () -> {
            String raw = "a\"b\\c\n\t中文";
            String json = Json.write(Map.of("s", raw));
            @SuppressWarnings("unchecked")
            Map<String, Object> back = (Map<String, Object>) Json.parse(json);
            checkEq(back.get("s"), raw, "转义往返");
            check(json.contains("中文"), "中文不转义");
        });
    }

    private void testJsonInvalid() {
        test("JSON：非法输入抛 JsonException", () -> {
            boolean thrown = false;
            try {
                Json.parse("{\"a\":}");
            } catch (Json.JsonException e) {
                thrown = true;
            }
            check(thrown, "非法 JSON 应抛异常");
        });
    }

    private void testResponseSerializes() {
        test("JSON：完整服务响应可序列化为合法 JSON 并解析回来", () -> {
            SegService.Result r = service.segment("v1", "研究生命X", 3);
            String json = Json.write(r.body);
            Object parsed = Json.parse(json);
            check(parsed instanceof Map, "解析回对象");
            @SuppressWarnings("unchecked")
            Map<String, Object> m = (Map<String, Object>) parsed;
            checkEq(m.get("dictionary"), "v1", "字段存在");
        });
    }
}
