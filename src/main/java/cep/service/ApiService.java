package cep.service;

import cep.config.LatePolicy;
import cep.config.MatchPolicy;
import cep.config.PatternConfig;
import cep.json.JsonValue;
import cep.model.EngineResult;
import cep.model.KeyEvent;
import cep.model.Match;
import cep.model.Timeout;
import cep.pattern.BruteForceMatcher;
import cep.pattern.PatternEngine;
import cep.pattern.ReferenceResult;

import java.util.ArrayList;
import java.util.List;

/** 把 JSON 请求映射为引擎调用、把引擎结果映射回 JSON（纯函数，便于直接单测）。 */
public final class ApiService {

    private ApiService() {}

    // ----------------------------------------------------------- 配置解析

    static PatternConfig parseConfig(JsonValue.Obj req) {
        JsonValue.Obj c;
        if (req.has("pattern")) {
            c = req.requireObj("pattern");
        } else {
            // 允许把配置平铺在顶层
            c = req;
        }
        String a = c.requireString("a");
        String b = c.requireString("b");
        String cc = c.requireString("c");
        long window = c.requireLong("windowMs");
        MatchPolicy policy = MatchPolicy.valueOf(
                c.optString("policy", "ALL_PAIRS"));
        LatePolicy latePolicy = LatePolicy.valueOf(
                c.optString("latePolicy", "DROP"));
        long bound = c.optLong("outOfOrderBound", 0L);
        long allowedLateness = c.optLong("allowedLateness", 0L);
        boolean emitTimeouts = c.optBool("emitTimeouts", true);
        try {
            return new PatternConfig(a, b, cc, window, policy, latePolicy,
                    bound, allowedLateness, emitTimeouts);
        } catch (IllegalArgumentException ex) {
            throw ApiException.badRequest("非法配置: " + ex.getMessage());
        }
    }

    // ----------------------------------------------------------- 一次性评估

    /** POST /evaluate：可重放的确定性计算。 */
    public static JsonValue.Obj evaluate(JsonValue.Obj req) {
        PatternConfig cfg = parseConfig(req);
        JsonValue.Arr events = req.requireArr("events");
        boolean useReference = req.optBool("useReference", false);

        PatternEngine engine = new PatternEngine(cfg);
        List<KeyEvent> referenceInput = useReference ? new ArrayList<>() : null;

        long arrival = 0;
        for (JsonValue raw : events.values()) {
            JsonValue.Obj ev = asEvent(raw);
            String id = ev.requireString("id");
            String key = ev.requireString("key");
            long ts = ev.requireLong("timestamp");
            boolean seqProvided = ev.has("seq");
            long seq = seqProvided ? ev.requireLong("seq") : arrival;
            try {
                if (seqProvided) {
                    engine.ingest(id, key, ts, seq);
                } else {
                    engine.ingest(id, key, ts);
                }
            } catch (IllegalArgumentException ex) {
                throw ApiException.badRequest(ex.getMessage());
            }
            if (useReference) {
                referenceInput.add(new KeyEvent(id, key, ts, seq));
            }
            arrival++;
        }

        EngineResult finalResult;
        ReferenceResult ref = null;
        if (useReference) {
            ref = BruteForceMatcher.evaluate(cfg, referenceInput);
        }
        finalResult = engine.flush();

        JsonValue.Obj out = resultJson(finalResult, true);
        if (useReference) {
            out.set("reference", referenceJson(ref));
            out.set("agreesWithReference", resultAgrees(finalResult, ref));
        }
        out.set("mode", useReference ? "streaming+reference" : "streaming");
        return out;
    }

    // ----------------------------------------------------------- JSON 映射

    static JsonValue.Obj matchJson(Match m) {
        return JsonValue.obj()
                .set("aId", m.aId())
                .set("bId", m.bId())
                .set("aTimestamp", m.aTimestamp())
                .set("bTimestamp", m.bTimestamp())
                .set("durationMs", m.bTimestamp() - m.aTimestamp())
                .set("matchedAtWatermark",
                        m.matchedAtWatermark() == Long.MIN_VALUE ? JsonValue.nul()
                                : JsonValue.of(m.matchedAtWatermark()))
                .set("late", m.late());
    }

    static JsonValue.Obj timeoutJson(Timeout t) {
        return JsonValue.obj()
                .set("aId", t.aId())
                .set("aTimestamp", t.aTimestamp())
                .set("deadline", t.deadline())
                .set("late", t.late());
    }

    static JsonValue.Obj resultJson(EngineResult r, boolean flushed) {
        JsonValue.Arr ms = JsonValue.arr();
        r.matches().forEach(m -> ms.add(matchJson(m)));
        JsonValue.Arr ts = JsonValue.arr();
        r.timeouts().forEach(t -> ts.add(timeoutJson(t)));
        JsonValue.Obj stats = JsonValue.obj()
                .set("received", r.stats().received)
                .set("processed", r.stats().processed)
                .set("duplicates", r.stats().duplicates)
                .set("droppedLate", r.stats().droppedLate)
                .set("acceptedLate", r.stats().acceptedLate)
                .set("matches", r.stats().matches)
                .set("timeouts", r.stats().timeouts)
                .set("cKilled", r.stats().cKilled)
                .set("watermarks", r.stats().watermarks);
        return JsonValue.obj()
                .set("matches", ms)
                .set("timeouts", ts)
                .set("watermark",
                        r.watermark() == Long.MIN_VALUE ? JsonValue.nul()
                                : JsonValue.of(r.watermark()))
                .set("flushed", flushed)
                .set("stats", stats);
    }

    private static JsonValue.Obj referenceJson(ReferenceResult r) {
        JsonValue.Arr ms = JsonValue.arr();
        r.matches().forEach(m -> ms.add(matchJson(m)));
        JsonValue.Arr ts = JsonValue.arr();
        r.timeouts().forEach(t -> ts.add(timeoutJson(t)));
        return JsonValue.obj()
                .set("matches", ms)
                .set("timeouts", ts)
                .set("matchCount", r.matchCount())
                .set("timeoutCount", r.timeoutCount());
    }

    /** 流式结果与参考实现在匹配对、超时集合上是否一致（matchedAtWatermark 不参与比较）。 */
    static boolean resultAgrees(EngineResult s, ReferenceResult r) {
        if (s.matches().size() != r.matches().size()
                || s.timeouts().size() != r.timeouts().size()) {
            return false;
        }
        for (int i = 0; i < s.matches().size(); i++) {
            Match a = s.matches().get(i);
            Match b = r.matches().get(i);
            if (!a.aId().equals(b.aId()) || !a.bId().equals(b.bId())) {
                return false;
            }
        }
        for (int i = 0; i < s.timeouts().size(); i++) {
            if (!s.timeouts().get(i).aId().equals(r.timeouts().get(i).aId())) {
                return false;
            }
        }
        return true;
    }

    private static JsonValue.Obj asEvent(JsonValue raw) {
        if (!(raw instanceof JsonValue.Obj ev)) {
            throw ApiException.badRequest("events 中每一项必须是对象");
        }
        return ev;
    }
}
