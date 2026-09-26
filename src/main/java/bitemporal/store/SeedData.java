package bitemporal.store;

import bitemporal.model.ChangeRequest;
import bitemporal.model.TransactionRequest;

import java.time.Instant;
import java.util.List;

/**
 * 本地固定测试数据：不连任何外部数据源，所有时刻均为写死的 UTC 时间，
 * 便于按手算结果核对。
 *
 * <p>场景：两条“员工职级记录”（仅作普通双时态数据演示，本系统不是预约或
 * 考勤系统）。所有初始行都在 {@code 2026-01-01T00:00:00Z} 这一系统时刻入库，
 * 业务有效时间从 2026 年各季度开始。</p>
 */
public final class SeedData {

    public static final Instant SEED_COMMIT_AT = Instant.parse("2026-01-01T00:00:00Z");

    private SeedData() {
    }

    /** 构造种子事务：emp-1001 的 Q1~Q3 三段记录 + emp-1002 的 Q1 起开放记录。 */
    public static TransactionRequest seedTransaction() {
        Instant q1 = Instant.parse("2026-01-01T00:00:00Z");
        Instant q2 = Instant.parse("2026-04-01T00:00:00Z");
        Instant q3 = Instant.parse("2026-07-01T00:00:00Z");

        List<ChangeRequest> changes = List.of(
                new ChangeRequest("insert", "emp-1001",
                        "{\"level\":\"L3\",\"team\":\"Platform\"}", q1, q2),
                new ChangeRequest("insert", "emp-1001",
                        "{\"level\":\"L4\",\"team\":\"Platform\"}", q2, q3),
                new ChangeRequest("insert", "emp-1001",
                        "{\"level\":\"L4\",\"team\":\"Infra\"}", q3, null),
                new ChangeRequest("insert", "emp-1002",
                        "{\"level\":\"L2\",\"team\":\"Data\"}", q1, null));
        return new TransactionRequest("seed-txn", SEED_COMMIT_AT, changes);
    }
}
