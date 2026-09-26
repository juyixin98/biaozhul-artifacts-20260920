package com.example.bitemporal.data;

import com.example.bitemporal.engine.BitemporalStore;
import com.example.bitemporal.model.ChangeRequest;
import com.example.bitemporal.model.WriteMode;

import java.time.LocalDate;
import java.util.List;

/**
 * 本地固定测试数据：项目成员的部门/岗位归属历史（与预约、考勤无关）。
 *
 * <p>初始装载事务时间 = 2026-01-01。装载后各实体在业务时间轴上的当前版本：
 * <pre>
 * E001 张伟: Engineering/Dev       [2025-01-01, 2025-07-01)
 *           Engineering/TechLead  [2025-07-01, +inf)
 * E002 李娜: Sales/Rep            [2025-01-01, 2025-10-01)
 *           Sales/Manager        [2025-10-01, +inf)
 * E003 王芳: Finance/Analyst      [2025-03-01, +inf)
 * </pre>
 * 相邻区间端点相接（半开区间下不算重叠），可在同一事务内一起插入。
 */
public final class SeedData {

    public static final LocalDate SEED_TX_DATE = LocalDate.of(2026, 1, 1);

    private SeedData() {
    }

    public static BitemporalStore createSeededStore() {
        BitemporalStore store = new BitemporalStore();
        store.commit(SEED_TX_DATE, seedChanges());
        return store;
    }

    public static List<ChangeRequest> seedChanges() {
        return List.of(
                new ChangeRequest("E001", "Engineering", "Dev",
                        LocalDate.of(2025, 1, 1), LocalDate.of(2025, 7, 1), WriteMode.INSERT),
                new ChangeRequest("E001", "Engineering", "TechLead",
                        LocalDate.of(2025, 7, 1), null, WriteMode.INSERT),
                new ChangeRequest("E002", "Sales", "Rep",
                        LocalDate.of(2025, 1, 1), LocalDate.of(2025, 10, 1), WriteMode.INSERT),
                new ChangeRequest("E002", "Sales", "Manager",
                        LocalDate.of(2025, 10, 1), null, WriteMode.INSERT),
                new ChangeRequest("E003", "Finance", "Analyst",
                        LocalDate.of(2025, 3, 1), null, WriteMode.INSERT));
    }
}
