package topk;

import java.util.List;

/**
 * 验收场景：手工构造事件序列，每一步都对照窗口内完整排序（fullOrder）。
 *
 * 覆盖：并列名次（同分按 ID）、负增量、K 大于元素数、窗口滑出、
 * 撤回幂等（过期/撤回只扣一次）、迟到事件、分组隔离。
 */
public final class AcceptanceTest {

    private static final long W = 10_000; // 窗口 10s

    public static void main(String[] args) {
        TestHarness t = new TestHarness("验收场景（手工构造）");

        t.add("并列名次：同分按 itemId 字典序，TopK 与完整排序前 K 一致", () -> {
            GroupState g = new GroupState(W);
            g.insert(new Event("e1", "beta", 5, 1000));
            g.insert(new Event("e2", "alpha", 5, 1001));
            g.insert(new Event("e3", "gamma", 5, 1002));
            g.insert(new Event("e4", "delta", 8, 1003));
            g.advance(2000);
            // delta=8 最高；beta/alpha/gamma 同为 5，按 ID 升序：alpha beta gamma
            TestHarness.eqRows(g.fullOrder(), "[delta=8,alpha=5,beta=5,gamma=5]", "完整排序");
            TestHarness.eqRows(g.topK(2), "[delta=8,alpha=5]", "K=2 取前两名");
            TestHarness.eqRows(g.topK(3), "[delta=8,alpha=5,beta=5]", "K=3");
            // 逐条核对 rank
            List<GroupState.Row> top = g.topK(10);
            TestHarness.eq(top.get(0).itemId, "delta", "rank1");
            TestHarness.eq(top.get(1).itemId, "alpha", "rank2 并列时 ID 小的在前");
            TestHarness.eq(top.get(2).itemId, "beta", "rank3");
            TestHarness.eq(top.get(3).itemId, "gamma", "rank4");
        });

        t.add("负增量：分数可被减为负，负分仍按 (-score, id) 精确排序", () -> {
            GroupState g = new GroupState(W);
            g.insert(new Event("e1", "a", 10, 1000));
            g.insert(new Event("e2", "a", -7, 1001));   // a -> 3
            g.insert(new Event("e3", "b", 3, 1002));    // b = 3，与 a 并列，a ID 小在前
            g.insert(new Event("e4", "c", -2, 1003));   // c = -2 垫底
            g.insert(new Event("e5", "a", -10, 1004));  // a -> -7，跌到 c 后面
            g.advance(2000);
            TestHarness.eqRows(g.fullOrder(), "[b=3,c=-2,a=-7]", "完整排序含负分");
            TestHarness.eq(g.scoreOf("a"), -7L, "a 总分");
        });

        t.add("K 大于元素数 / K=0：返回全部或空，不报错", () -> {
            GroupState g = new GroupState(W);
            g.insert(new Event("e1", "a", 1, 1000));
            g.insert(new Event("e2", "b", 2, 1001));
            g.advance(2000);
            TestHarness.eq(g.topK(100).size(), 2, "K 大于元素数 -> 全部 2 个");
            TestHarness.eq(g.topK(0).size(), 0, "K=0 -> 空列表");
            GroupState empty = new GroupState(W);
            TestHarness.eq(empty.topK(5).size(), 0, "空组 K=5 -> 空列表");
        });

        t.add("窗口滑出：推进水位后过期事件恰好扣减一次且移出内存", () -> {
            GroupState g = new GroupState(W);
            g.insert(new Event("e1", "a", 10, 1000));
            g.insert(new Event("e2", "b", 5, 2000));
            g.advance(5000);
            TestHarness.eqRows(g.fullOrder(), "[a=10,b=5]", "t=5000 都在窗内");
            // 水位推到 11000：左边界 1000，e1(ts=1000) 滑出；a 的 10 分只扣一次
            g.advance(11_000);
            TestHarness.eqRows(g.fullOrder(), "[b=5]", "t=11000 e1 滑出");
            TestHarness.eq(g.activeEventCount(), 1, "内存中只剩 1 条有效事件");
            TestHarness.eq(g.scoreOf("a"), 0L, "a 分数归零已移除");
            TestHarness.eq(g.activeItemCount(), 1, "a 已退出排名");
            // 再滑出 b
            g.advance(12_001);
            TestHarness.eq(g.fullOrder().size(), 0, "全部滑出 -> 空排名");
            TestHarness.eq(g.activeEventCount(), 0, "不保留历史事件");
        });

        t.add("撤回：扣减一次、幂等重复撤回不二次扣减、撤回后过期不再扣", () -> {
            GroupState g = new GroupState(W);
            g.insert(new Event("e1", "a", 10, 1000));
            g.insert(new Event("e2", "a", -4, 2000));  // a = 6
            g.insert(new Event("e3", "b", 3, 3000));
            g.advance(5000);
            TestHarness.eqRows(g.fullOrder(), "[a=6,b=3]", "撤回前");

            TestHarness.eq(g.retract("e1", 6000), GroupState.RetractStatus.RETRACTED, "首次撤回成功");
            TestHarness.eq(g.scoreOf("a"), -4L, "撤回 e1：a 只剩 -4");
            TestHarness.eqRows(g.fullOrder(), "[b=3,a=-4]", "撤回后完整排序");
            TestHarness.eq(g.activeEventCount(), 2, "e1 已从有效事件删除");

            TestHarness.eq(g.retract("e1", 6001), GroupState.RetractStatus.ALREADY_RETRACTED,
                    "重复撤回幂等");
            TestHarness.eq(g.scoreOf("a"), -4L, "重复撤回不二次扣减");
            TestHarness.eqRows(g.fullOrder(), "[b=3,a=-4]", "分数未再变化");

            // e1 已撤回；推进到它本应过期之后，也绝不能再扣
            g.advance(12_000); // 左边界 2000：e2(ts=2000) 滑出
            TestHarness.eq(g.scoreOf("a"), 0L, "e2 过期把 a 拉回 0，a 退出排名");
            TestHarness.eqRows(g.fullOrder(), "[b=3]", "e1 撤回 + e2 过期各扣一次");
            // 墓碑此时已清（e1 ts=1000 <= 2000），同 ID 可复用
            TestHarness.eq(g.tombstoneCount(), 0, "墓碑随窗口滑出被清理，内存有界");
        });

        t.add("撤回不存在 / 已过期事件：EVENT_UNKNOWN，不改动任何分数", () -> {
            GroupState g = new GroupState(W);
            g.insert(new Event("e1", "a", 10, 1000));
            g.advance(11_000); // e1 已过期
            TestHarness.eq(g.retract("e1", 11_000), GroupState.RetractStatus.EVENT_UNKNOWN,
                    "过期事件不可撤回");
            TestHarness.eq(g.retract("ghost", 11_000), GroupState.RetractStatus.EVENT_UNKNOWN,
                    "根本不存在的事件不可撤回");
            TestHarness.eq(g.activeEventCount(), 0, "没有产生任何状态");
        });

        t.add("迟到事件（ts 已在窗口外）被拒：LATE 且不占内存", () -> {
            GroupState g = new GroupState(W);
            g.advance(10_000); // 左边界 0
            TestHarness.eq(g.insert(new Event("late", "a", 10, 0)), GroupState.InsertStatus.LATE,
                    "ts 恰在边界上 -> 过期，拒绝");
            TestHarness.eq(g.insert(new Event("late2", "a", 10, 1)), GroupState.InsertStatus.INSERTED,
                    "ts=1 在窗内，接受");
            TestHarness.eq(g.activeEventCount(), 1, "迟到事件不占内存");
            // 乱序但在窗内允许：先 ts=9000 再 ts=8000
            TestHarness.eq(g.insert(new Event("o1", "b", 1, 9000)), GroupState.InsertStatus.INSERTED, "");
            TestHarness.eq(g.insert(new Event("o2", "b", 2, 8000)), GroupState.InsertStatus.INSERTED,
                    "窗内乱序插入允许");
        });

        t.add("重复 eventId：DUPLICATE；墓碑保留期内重复，保留期外 ID 可复用", () -> {
            GroupState g = new GroupState(W);
            TestHarness.eq(g.insert(new Event("e1", "a", 10, 1000)), GroupState.InsertStatus.INSERTED, "");
            TestHarness.eq(g.insert(new Event("e1", "a", 5, 1001)), GroupState.InsertStatus.DUPLICATE,
                    "窗口内重复 ID 拒绝");
            TestHarness.eq(g.scoreOf("a"), 10L, "重复插入未生效");
            g.retract("e1", 2000);
            TestHarness.eq(g.insert(new Event("e1", "a", 7, 2001)), GroupState.InsertStatus.DUPLICATE,
                    "墓碑保留期内 ID 复用拒绝，防止撤回旧事件误伤新事件");
            g.advance(12_000); // 墓碑过期
            TestHarness.eq(g.insert(new Event("e1", "c", 7, 12_000)), GroupState.InsertStatus.INSERTED,
                    "墓碑滑出后 ID 可复用");
            TestHarness.eqRows(g.fullOrder(), "[c=7]", "复用后是全新事件");
        });

        t.add("同一 item 事件全部撤回/过期到 0 条时退出排名；之后重新出现再入榜", () -> {
            GroupState g = new GroupState(W);
            g.insert(new Event("e1", "a", 10, 1000));
            g.insert(new Event("e2", "a", -10, 2000)); // 总分 0 但有 2 条事件，仍在榜
            g.advance(3000);
            TestHarness.eq(g.activeItemCount(), 1, "总分 0 但事件仍在 -> 保留在榜");
            TestHarness.eqRows(g.fullOrder(), "[a=0]", "0 分也在榜");
            g.retract("e1", 4000);
            TestHarness.eq(g.scoreOf("a"), -10L, "撤 e1 后 a=-10 仍在榜");
            g.retract("e2", 5000);
            TestHarness.eq(g.activeItemCount(), 0, "有效事件归零 -> 退出排名");
            g.insert(new Event("e3", "a", 99, 5000));
            TestHarness.eqRows(g.fullOrder(), "[a=99]", "重新出现 -> 重新入榜，分数不残留");
        });

        t.add("分组隔离：两组各自窗口/水位/排名互不影响", () -> {
            TopKService svc = new TopKService(W);
            svc.insert("g1", new Event("e1", "a", 10, 1000), 1000);
            svc.insert("g2", new Event("e1", "a", 1, 1000), 1000);
            svc.group("g1").advance(12_000); // g1 的 e1 过期
            TestHarness.eq(svc.topK("g1", 10, 12_000L).size(), 0, "g1 已空");
            TestHarness.eq(svc.topK("g2", 10, 2000L).get(0).score, 1L, "g2 不受 g1 水位影响");
        });

        System.exit(t.run());
    }
}
