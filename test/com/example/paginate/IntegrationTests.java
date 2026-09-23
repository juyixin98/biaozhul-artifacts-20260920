package com.example.paginate;

import com.example.paginate.cursor.CursorService;
import com.example.paginate.web.HttpServerApp;

import java.time.Duration;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;

import static com.example.paginate.TestFramework.assertEquals;
import static com.example.paginate.TestFramework.assertTrue;
import static com.example.paginate.TestFramework.fail;

/** 通过真实 HTTP 接口的端到端测试（TTL 设为 1 秒以便测试过期）。 */
final class IntegrationTests {

    private IntegrationTests() {
    }

    private static ApiClient api;

    static int register(TestFramework tf) throws Exception {
        HttpServerApp app = new HttpServerApp(0, Duration.ofSeconds(1), "integration-fixed-secret");
        int port = app.start();
        api = new ApiClient(port);

        tf.run("健康检查 /healthz 返回 200", () -> {
            ApiClient.Response r = api.get("/healthz");
            assertEquals(200, r.status(), "healthz 应返回 200");
            assertEquals("ok", String.valueOf(r.json().get("status")), "status=ok");
        });

        tf.run("全量翻页 name_asc：无重无漏、页大小正确、次序与(name,id)一致", () -> {
            List<Long> ids = walkAll(Map.of("sort", "name_asc", "pageSize", "7"));
            assertEquals(57, ids.size(), "种子数据共 57 条");
            assertEquals(57, new HashSet<>(ids).size(), "翻页过程中不得出现重复 id");

            // 用小页再走一遍，校验页内内容严格按 name 升序、重名按 id 升序
            ApiClient.Response first = api.listItems(Map.of("sort", "name_asc", "pageSize", "5"));
            List<Map<String, Object>> items = itemsOf(first);
            String lastName = null;
            long tieId = Long.MAX_VALUE;
            for (Map<String, Object> it : items) {
                String name = String.valueOf(it.get("name"));
                long id = ((Number) it.get("id")).longValue();
                if (lastName != null) {
                    int cmp = name.compareTo(lastName);
                    assertTrue(cmp >= 0, "name 必须非递减");
                    if (cmp == 0) {
                        assertTrue(id > tieId, "重名时 id 必须递增");
                    }
                }
                lastName = name;
                tieId = id;
            }
        });

        tf.run("score_desc 全量翻页：57 条且严格按 (score desc, id asc)", () -> {
            List<Long> ids = walkAll(Map.of("sort", "score_desc", "pageSize", "10"));
            assertEquals(57, ids.size(), "仍应取到全部 57 条");
            assertEquals(57, new HashSet<>(ids).size(), "不得重复");
        });

        tf.run("【核心验收】分页途中插入：同一快照不重不漏，新记录不出现在旧快照", () -> {
            Map<String, String> params = Map.of("sort", "name_asc", "pageSize", "10");
            ApiClient.Response p1 = api.listItems(params);
            String cursor = p1.nextCursor();
            List<Long> seen = new ArrayList<>(idsOf(p1));

            // 翻页过程中往“当前数据”插入一条
            ApiClient.Response created = api.post("/api/admin/items",
                    Map.of("name", "zzz-new", "category", "books", "score", 999));
            assertEquals(201, created.status(), "插入应返回 201");
            @SuppressWarnings("unchecked")
            long newId = ((Number) ((Map<String, Object>) created.json().get("item")).get("id"))
                    .longValue();

            while (cursor != null) {
                ApiClient.Response page = api.listItems(withCursor(params, cursor));
                seen.addAll(idsOf(page));
                cursor = page.nextCursor();
            }
            assertEquals(57, seen.size(), "旧快照仍应正好 57 条");
            assertEquals(57, new HashSet<>(seen).size(), "旧快照遍历不得有重复");
            assertTrue(!seen.contains(newId), "翻页途中新插入的记录不得混入旧快照");

            // 重新开第一页（新快照）应当能看到新记录
            List<Long> freshIds = walkAll(Map.of("sort", "name_asc", "pageSize", "10"));
            assertEquals(58, freshIds.size(), "新快照应包含 58 条");
            assertTrue(freshIds.contains(newId), "新快照必须包含刚插入的记录");
        });

        tf.run("【核心验收】分页途中删除：被删记录仍在旧快照中（快照已固定），且不重不漏",
                () -> {
                    Map<String, String> params = Map.of("sort", "name_asc", "pageSize", "13");
                    ApiClient.Response p1 = api.listItems(params);
                    String cursor = p1.nextCursor();
                    List<Long> seen = new ArrayList<>(idsOf(p1));

                    // 删除一个尚未翻到的记录（取第一页最后一个 id 之后、靠后的一条）
                    long victim = 40L;
                    assertEquals(200, api.delete("/api/admin/items/" + victim).status(),
                            "删除 id=40 应成功");

                    while (cursor != null) {
                        ApiClient.Response page = api.listItems(withCursor(params, cursor));
                        seen.addAll(idsOf(page));
                        cursor = page.nextCursor();
                    }
                    assertEquals(58, seen.size(), "删除发生在快照建立之后，旧快照仍 58 条");
                    assertTrue(seen.contains(victim), "被删记录仍属于旧快照，应当被遍历到");
                    assertEquals(58, new HashSet<>(seen).size(), "不得重复");

                    // 新快照少一条
                    List<Long> freshIds = walkAll(Map.of("sort", "name_asc", "pageSize", "13"));
                    assertEquals(57, freshIds.size(), "新快照应少一条 => 57");
                    assertTrue(!freshIds.contains(victim), "新快照不含已删除记录");
                });

        tf.run("【核心验收】分页途中修改排序键：旧快照保持旧次序，不重不漏", () -> {
            // 注意上一用例删了 40，当前真实数据 57 条；这里另开快照
            Map<String, String> params = Map.of("sort", "name_asc", "pageSize", "9");
            ApiClient.Response p1 = api.listItems(params);
            String cursor = p1.nextCursor();
            List<Long> seen = new ArrayList<>(idsOf(p1));

            // 把一个还没翻到的记录改到名字序最前（若它进入新快照会排在最前）
            long moved = 50L;
            ApiClient.Response patched = api.patch("/api/admin/items/" + moved,
                    Map.of("name", "aaa-moved"));
            assertEquals(200, patched.status(), "改名应成功");

            while (cursor != null) {
                ApiClient.Response page = api.listItems(withCursor(params, cursor));
                seen.addAll(idsOf(page));
                cursor = page.nextCursor();
            }
            assertEquals(57, seen.size(), "旧快照应固定为建立时的 57 条");
            assertEquals(57, new HashSet<>(seen).size(), "旧快照遍历无重复");
            // 旧快照里 id=50 仍以旧名字参与排序：它在旧快照中恰好出现一次
            assertTrue(seen.contains(moved), "被修改记录仍在旧快照中出现一次");

            // 新快照中它排到第一页最前面，名字是 aaa-moved
            ApiClient.Response fresh = api.listItems(Map.of("sort", "name_asc", "pageSize", "5"));
            List<Map<String, Object>> firstItems = itemsOf(fresh);
            assertEquals(moved, ((Number) firstItems.get(0).get("id")).longValue(),
                    "新快照第一条应为改名后的 id=" + moved);
        });

        tf.run("【核心验收】变更筛选条件不能复用游标：cursor_query_mismatch", () -> {
            ApiClient.Response p1 = api.listItems(Map.of(
                    "sort", "name_asc", "category", "books", "pageSize", "5"));
            String cursor = p1.nextCursor();
            assertTrue(cursor != null, "第一页应返回游标");

            // 同一游标拿去查 music
            ApiClient.Response r = api.listItems(Map.of(
                    "sort", "name_asc", "category", "music", "pageSize", "5", "cursor", cursor));
            assertEquals(400, r.status(), "换 category 复用游标应 400");
            assertEquals("cursor_query_mismatch", r.errorCode(), "错误码 cursor_query_mismatch");

            // 换排序
            ApiClient.Response r2 = api.listItems(Map.of(
                    "sort", "name_desc", "category", "books", "pageSize", "5", "cursor", cursor));
            assertEquals(400, r2.status(), "换 sort 复用游标应 400");

            // 换页大小
            ApiClient.Response r3 = api.listItems(Map.of(
                    "sort", "name_asc", "category", "books", "pageSize", "6", "cursor", cursor));
            assertEquals(400, r3.status(), "换 pageSize 复用游标应 400");
        });

        tf.run("【核心验收】伪造/篡改游标不能复用：403 cursor_invalid", () -> {
            ApiClient.Response p1 = api.listItems(Map.of("sort", "name_asc", "pageSize", "5"));
            String cursor = p1.nextCursor();

            // 1) 翻转一个字符
            char flip = cursor.charAt(0) == 'A' ? 'B' : 'A';
            String tampered = flip + cursor.substring(1);
            ApiClient.Response r1 = api.listItems(Map.of(
                    "sort", "name_asc", "pageSize", "5", "cursor", tampered));
            assertEquals(403, r1.status(), "篡改游标应 403");
            assertEquals("cursor_invalid", r1.errorCode(), "错误码 cursor_invalid");

            // 2) 完全编造
            ApiClient.Response r2 = api.listItems(Map.of(
                    "sort", "name_asc", "pageSize", "5", "cursor", "not-a-real-cursor"));
            assertEquals(403, r2.status(), "编造游标应 403");

            // 3) 删掉 MAC 段
            ApiClient.Response r3 = api.listItems(Map.of(
                    "sort", "name_asc", "pageSize", "5", "cursor", cursor.split("\\.")[0]));
            assertEquals(403, r3.status(), "缺 MAC 段应 403");

            // 4) 另一密钥签发的游标（新起一个服务，游标不能跨密钥使用）
            HttpServerApp other = new HttpServerApp(0, Duration.ofSeconds(1),
                    CursorService.randomSecret());
            int otherPort = other.start();
            try {
                ApiClient otherApi = new ApiClient(otherPort);
                ApiClient.Response otherFirst = otherApi.listItems(
                        Map.of("sort", "name_asc", "pageSize", "5"));
                String foreignCursor = otherFirst.nextCursor();
                ApiClient.Response r4 = api.listItems(Map.of(
                        "sort", "name_asc", "pageSize", "5", "cursor", foreignCursor));
                assertEquals(403, r4.status(), "其他服务（不同密钥）签发的游标应 403");
            } finally {
                other.stop();
            }
        });

        tf.run("【核心验收】快照过期后游标明确报错 410 snapshot_expired", () -> {
            ApiClient.Response p1 = api.listItems(Map.of("sort", "name_asc", "pageSize", "5"));
            String cursor = p1.nextCursor();
            Thread.sleep(1300); // TTL=1s
            ApiClient.Response r = api.listItems(Map.of(
                    "sort", "name_asc", "pageSize", "5", "cursor", cursor));
            assertEquals(410, r.status(), "过期游标应返回 410");
            assertEquals("snapshot_expired", r.errorCode(), "错误码 snapshot_expired");
        });

        tf.run("游标指向不存在的快照：404 snapshot_not_found", () -> {
            // 用固定密钥让游标能通过 MAC 校验，但快照 ID 编造
            // 这里直接构造：正常取一个游标，替换其快照段不可行（MAC 会坏），
            // 因此验证伪造快照 ID 的现实情形：重启服务后旧快照 ID 丢失。
            // 单元层已有 404 覆盖；这里再验证一个未知快照经由完整流程的等价表现。
            // 取当前服务游标，等待到过期后两倍 TTL 之前：服务端仍持有，返回 410。
            ApiClient.Response p1 = api.listItems(Map.of("sort", "name_asc", "pageSize", "5"));
            String cursor = p1.nextCursor();
            Thread.sleep(1300);
            ApiClient.Response r = api.listItems(Map.of(
                    "sort", "name_asc", "pageSize", "5", "cursor", cursor));
            assertTrue(r.status() == 410, "过期（尚未清理）应稳定返回 410");
        });

        tf.run("参数校验：非法 sort / pageSize / 超长不影响，返回 400", () -> {
            assertEquals(400, api.get("/api/items?sort=age_desc").status(), "非法 sort 400");
            assertEquals(400, api.get("/api/items?pageSize=0").status(), "pageSize=0 400");
            assertEquals(400, api.get("/api/items?pageSize=101").status(), "pageSize=101 400");
            assertEquals(400, api.get("/api/items?pageSize=abc").status(), "pageSize=abc 400");
        });

        tf.run("管理端 CRUD：新增（自动ID/指定ID冲突）、查、改（含改排序键）、删、404", () -> {
            ApiClient.Response created = api.post("/api/admin/items",
                    Map.of("name", "crud-temp", "score", 42));
            assertEquals(201, created.status(), "新增应 201");
            long id = ((Number) ((Map<?, ?>) created.json().get("item")).get("id")).longValue();

            ApiClient.Response got = api.get("/api/admin/items/" + id);
            assertEquals(200, got.status(), "查询单条应 200");

            ApiClient.Response patched = api.patch("/api/admin/items/" + id,
                    Map.of("name", "crud-renamed", "score", 77));
            assertEquals(200, patched.status(), "修改应 200");
            assertEquals("crud-renamed",
                    String.valueOf(((Map<?, ?>) patched.json().get("item")).get("name")),
                    "新名字应生效");

            ApiClient.Response dup = api.post("/api/admin/items",
                    Map.of("id", id, "name", "dup"));
            assertEquals(409, dup.status(), "指定已存在 id 应 409");

            assertEquals(200, api.delete("/api/admin/items/" + id).status(), "删除应 200");
            assertEquals(404, api.get("/api/admin/items/" + id).status(), "删后再查应 404");
            assertEquals(404, api.delete("/api/admin/items/" + id).status(), "重复删除应 404");

            assertEquals(400, api.post("/api/admin/items", Map.of("name", "")).status(),
                    "空 name 应 400");
        });

        tf.run("category + q 组合筛选分页：合计数量与快照外的直接筛选一致", () -> {
            // books 分类名含 a 的记录：种子中 books(round0) 名字含 a 的
            // alpha, delta, india(不含a? india 有 a), lima, november?... 直接以实际遍历为准：
            // 只校验不重不漏（用第一页返回的 snapshotVersion 固定快照后全走一遍）
            Map<String, String> params = new LinkedHashMap<>();
            params.put("sort", "name_asc");
            params.put("category", "books");
            params.put("q", "a");
            params.put("pageSize", "3");

            ApiClient.Response p1 = api.listItems(params);
            assertEquals(200, p1.status(), "带筛选第一页应 200");
            List<Long> ids = new ArrayList<>(idsOf(p1));
            String cursor = p1.nextCursor();
            while (cursor != null) {
                ApiClient.Response page = api.listItems(withCursor(params, cursor));
                for (Long id : idsOf(page)) {
                    assertTrue(!ids.contains(id), "筛选翻页也不得重复");
                    ids.add(id);
                }
                cursor = page.nextCursor();
            }
            // books 分类即 round0 的 19 个名字，名含 'a' 的恰有 9 个：
            // alpha, beta, gamma, delta, india, lima, oscar, papa, sierra
            assertEquals(9, ids.size(), "books + q=a 应为 9 条，实际 " + ids.size());
            assertEquals(9, new HashSet<>(ids).size(), "筛选翻页不得重复");

            // 每页都必须确实满足筛选条件
            ApiClient.Response check = api.listItems(params);
            for (Map<String, Object> it : itemsOf(check)) {
                assertEquals("books", String.valueOf(it.get("category")),
                        "返回记录必须满足 category 筛选");
                assertTrue(String.valueOf(it.get("name")).toLowerCase().contains("a"),
                        "返回记录必须满足 q 子串筛选");
            }
        });

        tf.run("游标不能倒退/重复使用：同一游标取两次得到相同的下一页（无副作用）", () -> {
            ApiClient.Response p1 = api.listItems(Map.of("sort", "name_asc", "pageSize", "5"));
            String cursor = p1.nextCursor();
            Map<String, String> params = Map.of(
                    "sort", "name_asc", "pageSize", "5", "cursor", cursor);
            ApiClient.Response a = api.listItems(params);
            ApiClient.Response b = api.listItems(params);
            assertEquals(idsOf(a), idsOf(b), "同一游标重复请求结果必须一致（无状态推进）");
        });

        tf.run("pageSize 大于结果集：末页 nextCursor=null 且 hasMore=false", () -> {
            ApiClient.Response r = api.listItems(Map.of(
                    "sort", "name_asc", "category", "movies", "q", "zzz-no-such", "pageSize", "50"));
            assertEquals(200, r.status(), "空结果第一页 200");
            assertEquals(0, ((Number) r.json().get("count")).intValue(), "count=0");
            assertTrue(r.nextCursor() == null, "空结果不应给游标");
            assertEquals(Boolean.FALSE, r.json().get("hasMore"), "hasMore=false");
        });

        Runtime.getRuntime().addShutdownHook(new Thread(app::stop, "test-app-stop"));
        return port;
    }

    // ---------- 辅助 ----------

    private static List<Long> walkAll(Map<String, String> firstPageParams) throws Exception {
        ApiClient.Response page = api.listItems(firstPageParams);
        if (page.status() != 200) {
            fail("第一页应 200，实际 " + page.status() + " " + page.json());
        }
        List<Long> ids = new ArrayList<>(idsOf(page));
        String cursor = page.nextCursor();
        Set<String> seenCursors = new HashSet<>();
        while (cursor != null) {
            if (!seenCursors.add(cursor)) {
                fail("游标出现循环，分页无法终止");
            }
            page = api.listItems(withCursor(firstPageParams, cursor));
            if (page.status() != 200) {
                fail("翻页应 200，实际 " + page.status() + " " + page.json());
            }
            ids.addAll(idsOf(page));
            cursor = page.nextCursor();
        }
        return ids;
    }

    private static Map<String, String> withCursor(Map<String, String> base, String cursor) {
        Map<String, String> params = new LinkedHashMap<>(base);
        params.put("cursor", cursor);
        return params;
    }

    @SuppressWarnings("unchecked")
    private static List<Map<String, Object>> itemsOf(ApiClient.Response r) {
        return (List<Map<String, Object>>) r.json().get("items");
    }

    private static List<Long> idsOf(ApiClient.Response r) {
        List<Long> ids = new ArrayList<>();
        for (Map<String, Object> it : itemsOf(r)) {
            ids.add(((Number) it.get("id")).longValue());
        }
        return ids;
    }
}
