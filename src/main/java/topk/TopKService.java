package topk;

import java.util.List;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;

/**
 * 按分组管理滑动窗口 TopK 的服务门面。
 *
 * <p>每个分组独立窗口、独立水位、独立加锁；分组之间互不影响。
 * 分组懒创建，全服务共用一个窗口长度（构造时指定）。
 */
public final class TopKService {

    private final long windowMs;
    private final ConcurrentHashMap<String, GroupState> groups = new ConcurrentHashMap<>();

    public TopKService(long windowMs) {
        if (windowMs <= 0) {
            throw new IllegalArgumentException("windowMs 必须为正数: " + windowMs);
        }
        this.windowMs = windowMs;
    }

    public long windowMs() {
        return windowMs;
    }

    public GroupState group(String groupId) {
        return groups.computeIfAbsent(groupId, g -> new GroupState(windowMs));
    }

    /** 插入（ts 用于推进该组水位）。 */
    public GroupState.InsertStatus insert(String groupId, Event e, long ts) {
        GroupState g = group(groupId);
        synchronized (g) {
            g.advance(ts);
            return g.insert(e);
        }
    }

    /** 撤回（幂等）。 */
    public GroupState.RetractStatus retract(String groupId, String eventId, long ts) {
        return group(groupId).retract(eventId, ts);
    }

    /** 查询 TopK；ts 非 null 时先推进水位。 */
    public List<GroupState.Row> topK(String groupId, int k, Long ts) {
        GroupState g = group(groupId);
        synchronized (g) {
            if (ts != null) {
                g.advance(ts);
            }
            return g.topK(k);
        }
    }

    /** 分组内部快照（可观测性）。 */
    public Map<String, Object> snapshot(String groupId) {
        return group(groupId).snapshot();
    }

    public int groupCount() {
        return groups.size();
    }
}
