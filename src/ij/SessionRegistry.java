package ij;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;

/** 会话注册表（线程安全）。 */
final class SessionRegistry {

    private final Map<String, JoinSession> sessions = new ConcurrentHashMap<>();

    /** 新建会话；已存在同名会话时抛 IllegalStateException。 */
    JoinSession create(String id, long lowerBound, long upperBound) {
        JoinSession s = new JoinSession(id, lowerBound, upperBound);
        if (sessions.putIfAbsent(id, s) != null) {
            throw new IllegalStateException("session already exists: " + id);
        }
        return s;
    }

    JoinSession get(String id) {
        return sessions.get(id);
    }

    boolean delete(String id) {
        return sessions.remove(id) != null;
    }

    List<String> list() {
        return new ArrayList<>(sessions.keySet());
    }
}
