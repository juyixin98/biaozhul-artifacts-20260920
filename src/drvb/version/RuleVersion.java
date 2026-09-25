package drvb.version;

import drvb.json.Json;
import drvb.rule.Predicate;
import drvb.rule.Predicates;

import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.Map;

/**
 * 一条不可变的过滤规则版本。
 *
 * <p>版本一经发布：谓词树、规约 JSON、创建时间均不可更改；任何"改规则"的操作
 * 都是发布新版本并绑定新生效区间，从不修改旧版本。谓词规约在构造时做深拷贝，
 * 调用方后续修改入参 Map 不影响已发布版本。
 */
public final class RuleVersion {

    private final String id;
    private final String name;
    private final long createdAt;
    private final Predicate predicate;
    private final Map<String, Object> predicateSpec;

    public RuleVersion(String id, String name, long createdAt, Map<String, Object> predicateSpec) {
        if (id == null || id.isEmpty()) {
            throw new IllegalArgumentException("规则版本 id 必填");
        }
        if (predicateSpec == null) {
            throw new IllegalArgumentException("predicateSpec 必填");
        }
        this.id = id;
        this.name = name;
        this.createdAt = createdAt;
        // 编译 + 深拷贝，保证发布后不可变（编译会对非法规约快速失败）
        this.predicate = Predicates.compile(predicateSpec);
        this.predicateSpec = Collections.unmodifiableMap(
                new LinkedHashMap<>(Json.asObject(Json.parse(Json.write(predicateSpec)))));
    }

    public String id() {
        return id;
    }

    public String name() {
        return name;
    }

    public long createdAt() {
        return createdAt;
    }

    public Predicate predicate() {
        return predicate;
    }

    public Map<String, Object> predicateSpec() {
        return predicateSpec;
    }

    public boolean matches(Map<String, Object> context) {
        return predicate.test(context);
    }
}
