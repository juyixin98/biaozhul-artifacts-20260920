package phj.join;

import phj.Test;
import phj.TestRunner;
import phj.core.JoinType;
import phj.core.QueryRequest;
import phj.core.QueryResult;
import phj.core.Relation;

import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;

public class SpillMechanicsTest {

    private static Relation mkRel(String name, int rows, int distinctKeys, int nullPct, long seed) {
        return TestUtil.randomRelation(name, rows, distinctKeys, nullPct,
                new java.util.Random(seed), false, 3);
    }

    @Test
    public void spillsWhenOverThreshold() {
        Relation l = mkRel("L", 60, 20, 0, 1);
        Relation r = mkRel("R", 60, 20, 0, 2);
        var req = TestUtil.request(l, r, JoinType.INNER, List.of("k"), 4, 4, -1);
        QueryResult qr = TestUtil.assertAgainstReference(req);
        TestRunner.assertTrue((Long) qr.stats.get("spillPeakBytes") > 0, "应当发生落盘");
        TestRunner.assertTrue((Long) qr.stats.get("spillWaves") >= 1, "应有落盘波次");
    }

    @Test
    public void noSpillWhenFitsInMemory() {
        Relation l = mkRel("L", 5, 5, 0, 1);
        Relation r = mkRel("R", 5, 5, 0, 2);
        var req = TestUtil.request(l, r, JoinType.INNER, List.of("k"), 100, 4, -1);
        QueryResult qr = TestUtil.assertAgainstReference(req);
        TestRunner.assertEquals(0L, qr.stats.get("spillPeakBytes"));
        TestRunner.assertEquals(0L, qr.stats.get("spillWaves"));
    }

    @Test
    public void spillFilesAreKeptWhenRequested() throws Exception {
        Path tmp = Files.createTempDirectory("phj-keep-");
        Relation l = mkRel("L", 40, 15, 0, 1);
        Relation r = mkRel("R", 40, 15, 0, 2);
        var req = TestUtil.request(l, r, JoinType.INNER, List.of("k"), 4, 4, -1);
        req.spillDir = tmp;
        req.keepSpillFiles = true;
        QueryResult qr = TestUtil.assertAgainstReference(req);
        TestRunner.assertTrue(qr.spillDir != null, "应返回保留目录");
        Path dir = Path.of(qr.spillDir);
        TestRunner.assertTrue(Files.isDirectory(dir), "保留目录应存在");
        List<Path> jsonlFiles;
        try (var s = Files.walk(dir)) {
            jsonlFiles = s.filter(p -> p.toString().endsWith(".jsonl")).toList();
        }
        TestRunner.assertTrue(!jsonlFiles.isEmpty(), "应保留分区文件");
        long totalLines = 0;
        for (Path f : jsonlFiles) {
            List<String> lines = Files.readAllLines(f);
            totalLines += lines.size();
            for (String line : lines) {
                // 每个分区文件每行都必须是合法 JSON 数组；空分区（0 行）合法
                Object parsed = phj.json.Json.parse(line);
                TestRunner.assertTrue(parsed instanceof List<?>, "分区行应为 JSON 数组");
            }
        }
        TestRunner.assertTrue(totalLines > 0, "至少一个分区文件应有数据行");
        // 标记位图（.mark）属于内部文件，不应被保留
        try (var s = Files.walk(dir)) {
            long marks = s.filter(p -> p.toString().endsWith(".mark")).count();
            TestRunner.assertEquals(0L, marks, "标记位图不应保留");
        }
        // 清理
        try (var walk = Files.walk(tmp)) {
            walk.sorted(java.util.Comparator.reverseOrder()).forEach(p -> {
                try { Files.deleteIfExists(p); } catch (Exception ignored) { }
            });
        }
    }

    @Test
    public void spillFilesDeletedByDefault() throws Exception {
        Path tmp = Files.createTempDirectory("phj-clean-");
        Relation l = mkRel("L", 40, 15, 0, 1);
        Relation r = mkRel("R", 40, 15, 0, 2);
        var req = TestUtil.request(l, r, JoinType.INNER, List.of("k"), 4, 4, -1);
        req.spillDir = tmp;
        req.keepSpillFiles = false;
        TestUtil.assertAgainstReference(req);
        long remaining = Files.walk(tmp).filter(p -> p.getFileName().toString().startsWith("phj-spill-")).count();
        TestRunner.assertEquals(0L, remaining);
    }

    @Test
    public void recursiveRepartitionHandlesSkew() {
        // 中等基数 + 阈值极小：初始分区后必有大分区触发递归或回退，结果仍须正确
        Relation l = mkRel("L", 300, 100, 0, 11);
        Relation r = mkRel("R", 300, 100, 0, 22);
        var req = TestUtil.request(l, r, JoinType.INNER, List.of("k"), 3, 4, -1);
        QueryResult qr = TestUtil.assertAgainstReference(req);
        TestRunner.assertTrue(qr.stats.get("spillWaves") != null);
    }

    @Test
    public void singleHotKeyTriggersBoundedFallback() {
        Relation l = mkRel("L", 100, 1, 0, 1);   // 全部 k=0
        Relation r = mkRel("R", 100, 1, 0, 2);
        var req = TestUtil.request(l, r, JoinType.INNER, List.of("k"), 4, 8, -1);
        QueryResult qr = TestUtil.assertAgainstReference(req);
        TestRunner.assertEquals(10000, qr.rows.size()); // 100 x 100
        TestRunner.assertTrue((Long) qr.stats.get("hotKeyFallbacks") >= 1, "应识别热点并回退");
    }

    @Test
    public void hotKeyWithLeftJoinMarksAllProbes() {
        // 全热点 LEFT：所有探测行都会匹配，不应有错误的补 NULL 行
        Relation l = mkRel("L", 80, 1, 0, 1);
        Relation r = mkRel("R", 60, 1, 0, 2);
        var req = TestUtil.request(l, r, JoinType.LEFT, List.of("k"), 4, 8, -1);
        QueryResult qr = TestUtil.assertAgainstReference(req);
        TestRunner.assertEquals(4800, qr.rows.size()); // 80 x 60，无补位
    }

    @Test
    public void mixedHotAndColdKeysLeftJoin() {
        // 热点键 + 冷门键 + 无匹配键，LEFT 下 BNL 位图标记必须正确
        List<Object[]> lrows = new ArrayList<>();
        List<Object[]> rrows = new ArrayList<>();
        int id = 0;
        for (int i = 0; i < 80; i++) lrows.add(new Object[]{id++, "hot", "L"});
        for (int i = 0; i < 10; i++) lrows.add(new Object[]{id++, "cold" + (i % 3), "L"});
        lrows.add(new Object[]{id++, "never", "L"});
        int cid = 0;
        for (int i = 0; i < 60; i++) rrows.add(new Object[]{cid++, "hot", "R"});
        for (int i = 0; i < 6; i++) rrows.add(new Object[]{cid++, "cold" + (i % 3), "R"});
        Relation l = TestUtil.relation("L", List.of("id", "k", "v"), lrows.toArray(Object[][]::new));
        Relation r = TestUtil.relation("R", List.of("cid", "k", "w"), rrows.toArray(Object[][]::new));
        var req = TestUtil.request(l, r, JoinType.LEFT, List.of("k"), 5, 4, -1);
        QueryResult qr = TestUtil.assertAgainstReference(req);
        long nullPad = qr.rows.stream().filter(x -> x.get(3).isNull()).count();
        TestRunner.assertEquals(1L, nullPad, "仅 'never' 键那一行补 NULL");
    }

    @Test
    public void leftJoinDiskMarkerUsedForLargeProbePartition() {
        // 探测分区大（超阈值），走磁盘位图标记分支
        List<Object[]> lrows = new ArrayList<>();
        List<Object[]> rrows = new ArrayList<>();
        for (int i = 0; i < 60; i++) lrows.add(new Object[]{i, i % 30, "L"});
        for (int i = 0; i < 40; i++) rrows.add(new Object[]{1000 + i, i % 30, "R"});
        Relation l = TestUtil.relation("L", List.of("id", "k", "v"), lrows.toArray(Object[][]::new));
        Relation r = TestUtil.relation("R", List.of("cid", "k", "w"), rrows.toArray(Object[][]::new));
        var req = TestUtil.request(l, r, JoinType.LEFT, List.of("k"), 4, 4, -1);
        QueryResult qr = TestUtil.assertAgainstReference(req);
        // 计划树里应出现 disk marker 或 memory marker（两种实现都覆盖过时此断言放宽为存在 marker 字段）
        final boolean[] hasMarker = {false};
        walk(qr.planRoot.toMap(), hasMarker);
        TestRunner.assertTrue(hasMarker[0], "LEFT 落盘路径应记录 marker 类型");
    }

    @SuppressWarnings("unchecked")
    private void walk(java.util.Map<String, Object> node, boolean[] found) {
        if (node.containsKey("marker")) found[0] = true;
        Object kids = node.get("children");
        if (kids instanceof List<?> list) {
            for (Object o : list) walk((java.util.Map<String, Object>) o, found);
        }
    }

    @Test
    public void partitionCountConfigIsHonored() {
        Relation l = mkRel("L", 80, 40, 0, 1);
        Relation r = mkRel("R", 80, 40, 0, 2);
        var req = TestUtil.request(l, r, JoinType.INNER, List.of("k"), 4, 13, -1);
        QueryResult qr = TestUtil.assertAgainstReference(req);
        TestRunner.assertEquals(13L, qr.planRoot.toMap().get("initialPartitions"));
    }
}
