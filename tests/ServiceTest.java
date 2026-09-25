package tests;

import streammatch.model.EngineConfig;
import streammatch.model.EngineMode;
import streammatch.model.LatePolicy;
import streammatch.model.MatchPolicy;
import streammatch.service.ApiException;
import streammatch.service.MatchingService;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/** 服务层测试：配置解析、事件校验、在线/重放一致性以及错误码。 */
public class ServiceTest extends TestCase {

    private static EngineConfig defaultCfg() {
        return new EngineConfig(EngineMode.EVENT_TIME, 100,
                MatchPolicy.ALL_CANDIDATES, 0L, LatePolicy.DROP);
    }

    private static Map<String, Object> event(String id, String key, String type, long ts) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("id", id);
        m.put("key", key);
        m.put("type", type);
        m.put("timestamp", ts);
        return m;
    }

    @SuppressWarnings("unchecked")
    private static List<Map<String, Object>> matchesOf(Map<String, Object> resp) {
        return (List<Map<String, Object>>) resp.get("matches");
    }

    @Override
    protected void run() {
        ingestAndMatch();
        validationErrors();
        duplicateIdRejected();
        replayConsistency();
        replayCatchesUpLateArrival();
        resetClearsState();
        skipPolicyViaService();
    }

    private void ingestAndMatch() {
        MatchingService svc = new MatchingService(defaultCfg());
        Map<String, Object> resp = svc.ingest(Map.of("events", List.of(
                event("A1", "k", "A", 0),
                event("B2", "k", "B", 50))));
        eq(matchesOf(resp).size(), 1, "SVC: 增量返回 1 个匹配");
        eq(matchesOf(resp).get(0).get("aId"), "A1", "SVC: aId=A1");
        eq(resp.get("totalMatches"), 1, "SVC: 累计匹配数");
        svc.close();
    }

    private void validationErrors() {
        MatchingService svc = new MatchingService(defaultCfg());
        expect400(() -> svc.ingest(Map.of()), "SVC: 缺 events 字段 -> 400");
        expect400(() -> svc.ingest(Map.of("events", List.of(Map.of("id", "x")))),
                "SVC: 事件缺字段 -> 400");
        expect400(() -> svc.ingest(Map.of("events", List.of(event("X", "k", "Z", 0)))),
                "SVC: 非法类型 Z -> 400");
        expect400(() -> svc.ingest(Map.of("events", List.of(event("X", "k", "A", -1),
                event("Y", "k", "B", 0), event("X", "k", "C", 1)))),
                "SVC: 批内重复 id -> 400");
        expect422(() -> svc.reconfigure(Map.of("mode", "PROCESSING_TIME")),
                "SVC: 运行期切换模式 -> 422 MODE_IMMUTABLE");
        svc.close();
    }

    private void duplicateIdRejected() {
        MatchingService svc = new MatchingService(defaultCfg());
        svc.ingest(Map.of("events", List.of(event("A1", "k", "A", 0))));
        expect422(() -> svc.ingest(Map.of("events", List.of(event("A1", "k", "B", 10)))),
                "SVC: 跨请求重复 id -> 422");
        svc.close();
    }

    private void replayConsistency() {
        MatchingService svc = new MatchingService(defaultCfg());
        svc.ingest(Map.of("events", List.of(
                event("A1", "k", "A", 0),
                event("A2", "k", "A", 10),
                event("C3", "k", "C", 40),
                event("A4", "k", "A", 60),
                event("B5", "k", "B", 90))));
        Map<String, Object> replay = svc.replay(Map.of());
        eq(replay.get("consistent"), Boolean.TRUE, "SVC: 有序输入重放与参考一致");
        svc.close();
    }

    // 乱序到达产生迟到丢弃；重放给出无迟到假设下的完整结果，且与参考实现一致
    @SuppressWarnings("unchecked")
    private void replayCatchesUpLateArrival() {
        MatchingService svc = new MatchingService(defaultCfg());
        svc.ingest(Map.of("events", List.of(
                event("A1", "k", "A", 0),
                event("B2", "k", "B", 80))));
        Map<String, Object> late = svc.ingest(Map.of("events", List.of(
                event("A3", "k", "A", 20))));
        eq(late.get("lateDropped"), List.of("A3"), "SVC: A3 在线被丢弃");

        Map<String, Object> state = svc.state();
        eq(((List<?>) state.get("matches")).size(), 1, "SVC: 在线只有 1 个匹配");

        Map<String, Object> replay = svc.replay(Map.of());
        eq(replay.get("consistent"), Boolean.TRUE, "SVC: 重放结果与参考实现一致");
        List<Map<String, Object>> refMatches = (List<Map<String, Object>>) replay.get("referenceMatches");
        eq(refMatches.size(), 1, "SVC: 默认重放只含在线接受的 A1/B2");

        // 显式提供完整事件集（含在线被丢弃的 A3）做补算：排序后 A1@0,A3@20,B2@80 -> 2 个匹配
        Map<String, Object> replayFull = svc.replay(Map.of("events", List.of(
                event("A1", "k", "A", 0),
                event("B2", "k", "B", 80),
                event("A3", "k", "A", 20))));
        eq(replayFull.get("consistent"), Boolean.TRUE, "SVC: 完整集重放与参考一致");
        List<Map<String, Object>> fullRef =
                (List<Map<String, Object>>) replayFull.get("referenceMatches");
        eq(fullRef.size(), 2, "SVC: 显式完整集重放补回 A3-B2，共 2 个");
        svc.close();
    }

    private void resetClearsState() {
        MatchingService svc = new MatchingService(defaultCfg());
        svc.ingest(Map.of("events", List.of(event("A1", "k", "A", 0), event("B2", "k", "B", 5))));
        Map<String, Object> after = svc.reset(Map.of());
        eq(((List<?>) after.get("matches")).size(), 0, "SVC: reset 清空匹配");
        eq(after.get("acceptedEventCount"), 0, "SVC: reset 清空事件");
        // reset 后同一 id 可再次使用
        Map<String, Object> again = svc.ingest(Map.of("events", List.of(event("A1", "k", "A", 0))));
        eq(again.get("processed"), 1, "SVC: reset 后 id 可复用");
        svc.close();
    }

    private void skipPolicyViaService() {
        EngineConfig cfg = new EngineConfig(EngineMode.EVENT_TIME, 100,
                MatchPolicy.SKIP_PAST_LAST, 0L, LatePolicy.DROP);
        MatchingService svc = new MatchingService(cfg);
        Map<String, Object> resp = svc.ingest(Map.of("events", List.of(
                event("A1", "k", "A", 0),
                event("A2", "k", "A", 10),
                event("B3", "k", "B", 50))));
        eq(matchesOf(resp).size(), 1, "SVC: SKIP 策略只输出 1 个匹配");
        eq(matchesOf(resp).get(0).get("aId"), "A1", "SVC: SKIP 取最早 A");
        svc.close();
    }

    private void expect400(Runnable r, String msg) {
        try {
            r.run();
            fail(msg + "（未抛异常）");
        } catch (ApiException ex) {
            eq(ex.status(), 400, msg + "（状态码）");
        }
    }

    private void expect422(Runnable r, String msg) {
        try {
            r.run();
            fail(msg + "（未抛异常）");
        } catch (ApiException ex) {
            eq(ex.status(), 422, msg + "（状态码）");
        }
    }
}
