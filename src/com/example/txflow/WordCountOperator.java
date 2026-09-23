package com.example.txflow;

import java.util.LinkedHashMap;
import java.util.Map;

/**
 * 有状态流算子：word count。
 *
 * 输入记录：{"text": "a b c", ...其它字段忽略}
 * 处理效果：对空白分词，累计每个词的计数。
 * 输出（每条输入恰好一条结果行）：
 *   {"inputOffset":3,"text":"a b","delta":{"a":1,"b":1},
 *    "stateAfter":{"a":2,"b":1}}
 *
 * 输出里带 inputOffset，便于验收时机械核对“无遗漏、无重复”。
 */
public class WordCountOperator {

    /** 初始算子状态。 */
    public Map<String, Object> initialState() {
        Map<String, Object> state = new LinkedHashMap<>();
        state.put("counts", new LinkedHashMap<String, Object>());
        return state;
    }

    /**
     * 在给定状态上处理一条记录，原地更新状态并返回该记录的输出行（JSON 对象）。
     *
     * @param state  当前状态（含 counts），会被原地更新
     * @param offset 该记录的输入 offset
     */
    @SuppressWarnings("unchecked")
    public Map<String, Object> processOne(Map<String, Object> state, String recordJson, long offset) {
        Map<String, Object> rec = Json.parseObject(recordJson);
        String text = Json.str(rec, "text", "");

        Map<String, Object> counts = (Map<String, Object>) state.get("counts");
        if (counts == null) {
            counts = new LinkedHashMap<>();
            state.put("counts", counts);
        }

        Map<String, Object> delta = new LinkedHashMap<>();
        String[] words = text.trim().isEmpty() ? new String[0] : text.trim().split("\\s+");
        for (String w : words) {
            long c = counts.containsKey(w) ? ((Number) counts.get(w)).longValue() : 0L;
            counts.put(w, c + 1);
            long d = delta.containsKey(w) ? ((Number) delta.get(w)).longValue() : 0L;
            delta.put(w, d + 1);
        }

        Map<String, Object> out = new LinkedHashMap<>();
        out.put("inputOffset", offset);
        out.put("text", text);
        out.put("delta", delta);
        // 输出嵌入处理后的完整状态快照，使每条可见输出自描述且可独立核对
        out.put("stateAfter", deepCopy(state));
        return out;
    }

    @SuppressWarnings("unchecked")
    static Map<String, Object> deepCopy(Map<String, Object> m) {
        Map<String, Object> copy = new LinkedHashMap<>();
        for (Map.Entry<String, Object> e : m.entrySet()) {
            Object v = e.getValue();
            if (v instanceof Map) {
                v = deepCopy((Map<String, Object>) v);
            }
            copy.put(e.getKey(), v);
        }
        return copy;
    }
}
