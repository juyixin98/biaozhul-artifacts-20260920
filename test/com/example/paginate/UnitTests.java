package com.example.paginate;

import com.example.paginate.cursor.CursorPayload;
import com.example.paginate.cursor.CursorService;
import com.example.paginate.model.Item;
import com.example.paginate.query.PageQuery;
import com.example.paginate.snapshot.Snapshot;
import com.example.paginate.snapshot.SnapshotManager;
import com.example.paginate.store.ItemStore;
import com.example.paginate.web.ApiException;

import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.time.ZoneOffset;
import java.util.List;
import java.util.Map;

import static com.example.paginate.TestFramework.assertEquals;
import static com.example.paginate.TestFramework.assertTrue;
import static com.example.paginate.TestFramework.fail;

/** 不启动服务器的直接单元测试。 */
final class UnitTests {

    private UnitTests() {
    }

    static void register(TestFramework tf) {

        tf.run("快照在 TTL 内可取，过期后抛 410，移除后抛 404", () -> {
            MutableClock clock = new MutableClock(Instant.parse("2026-09-23T00:00:00Z"));
            SnapshotManager mgr = new SnapshotManager(Duration.ofSeconds(10), clock);
            Snapshot snap = mgr.create(() -> List.of(new Item(1, "a", "x", 0)));
            assertEquals(snap.id(), mgr.require(snap.id()).id(), "TTL 内应取回同一快照");

            clock.advance(Duration.ofSeconds(10)); // 恰好到边界：createdAt+ttl <= now
            try {
                mgr.require(snap.id());
                fail("边界时刻应判定为过期");
            } catch (ApiException e) {
                assertEquals(410, e.httpStatus(), "过期应为 410");
                assertEquals("snapshot_expired", e.code(), "错误码应为 snapshot_expired");
            }
        });

        tf.run("不存在的快照 ID 返回 404 snapshot_not_found", () -> {
            SnapshotManager mgr = new SnapshotManager(Duration.ofMinutes(1));
            try {
                mgr.require("deadbeefdeadbeef");
                fail("不存在的快照应抛错");
            } catch (ApiException e) {
                assertEquals(404, e.httpStatus(), "应为 404");
                assertEquals("snapshot_not_found", e.code(), "错误码应为 snapshot_not_found");
            }
        });

        tf.run("游标改一个字符即 MAC 校验失败(403)", () -> {
            CursorService svc = new CursorService("test-secret-key");
            CursorPayload p = new CursorPayload("hash", "snap", "alpha", 7);
            String token = svc.encode(p);

            char flip = token.charAt(0) == 'A' ? 'B' : 'A';
            String tampered = flip + token.substring(1);
            try {
                svc.decode(tampered, "hash");
                fail("篡改游标必须被拒绝");
            } catch (ApiException e) {
                assertEquals(403, e.httpStatus(), "篡改应为 403");
                assertEquals("cursor_invalid", e.code(), "错误码应为 cursor_invalid");
            }
        });

        tf.run("伪造的游标（另一个密钥签发）被拒绝", () -> {
            CursorService signer = new CursorService("key-a");
            CursorService verifier = new CursorService("key-b");
            String token = signer.encode(new CursorPayload("hash", "snap", "alpha", 7));
            try {
                verifier.decode(token, "hash");
                fail("不同密钥签发的游标必须被拒绝");
            } catch (ApiException e) {
                assertEquals(403, e.httpStatus(), "伪造应为 403");
            }
        });

        tf.run("查询指纹不匹配返回 400 cursor_query_mismatch", () -> {
            CursorService svc = new CursorService("k");
            String token = svc.encode(new CursorPayload("hash-1", "snap", "alpha", 1));
            try {
                svc.decode(token, "hash-2");
                fail("换查询条件复用游标必须被拒绝");
            } catch (ApiException e) {
                assertEquals(400, e.httpStatus(), "查询不匹配应为 400");
                assertEquals("cursor_query_mismatch", e.code(), "错误码应为 cursor_query_mismatch");
            }
        });

        tf.run("查询指纹对参数敏感：改 sort/category/q/pageSize 即变化", () -> {
            PageQuery q1 = new PageQuery("name_asc", null, null, 10);
            PageQuery q2 = new PageQuery("name_desc", null, null, 10);
            PageQuery q3 = new PageQuery("name_asc", "books", null, 10);
            PageQuery q4 = new PageQuery("name_asc", null, "abc", 10);
            PageQuery q5 = new PageQuery("name_asc", null, null, 20);
            String h = q1.queryHash();
            assertTrue(!h.equals(q2.queryHash()), "sort 不同指纹必须不同");
            assertTrue(!h.equals(q3.queryHash()), "category 不同指纹必须不同");
            assertTrue(!h.equals(q4.queryHash()), "q 不同指纹必须不同");
            assertTrue(!h.equals(q5.queryHash()), "pageSize 不同指纹必须不同");
        });

        tf.run("排序键重复时由唯一 id 决胜，顺序全序且稳定", () -> {
            List<Item> data = List.of(
                    new Item(30, "same", "c", 50),
                    new Item(2, "same", "c", 50),
                    new Item(15, "same", "c", 50));
            PageQuery asc = new PageQuery("name_asc", null, null, 10);
            List<Item> sorted = data.stream().sorted(asc.comparator()).toList();
            assertEquals(2L, sorted.get(0).id(), "重名时 id 小的在前");
            assertEquals(15L, sorted.get(1).id(), "第二个应为 id=15");
            assertEquals(30L, sorted.get(2).id(), "第三个应为 id=30");

            // strictlyAfter 必须与比较器一致：锚点 15 之后只有 30
            List<Long> after = data.stream()
                    .filter(it -> asc.strictlyAfter(it, "same", 15))
                    .map(Item::id).sorted().toList();
            assertEquals(List.of(30L), after, "锚点之后严格只剩 id=30");

            // score_desc：同分也按 id 决胜（升序），不同分大的在前
            PageQuery scoreDesc = new PageQuery("score_desc", null, null, 10);
            List<Item> mixed = List.of(
                    new Item(5, "a", "c", 10),
                    new Item(6, "a", "c", 20),
                    new Item(7, "a", "c", 20));
            List<Long> order = mixed.stream().sorted(scoreDesc.comparator())
                    .map(Item::id).toList();
            assertEquals(List.of(6L, 7L, 5L), order, "score_desc: 20 分在前，同分按 id 升序");
        });

        tf.run("Store 快照与后续写入/修改/删除相互隔离", () -> {
            ItemStore store = new ItemStore(List.of(
                    new Item(1, "a", "c", 0), new Item(2, "b", "c", 0)));
            List<Item> snap = store.snapshotItems();

            store.create(3L, "c", "c", 0);
            store.update(1, "a-renamed", "c", 99L);
            store.delete(2);

            assertEquals(2, snap.size(), "旧快照应保持 2 条");
            assertEquals("a", snap.get(0).name(), "旧快照里的记录不被原地修改");
            assertEquals(0L, snap.get(0).score(), "旧快照里的 score 不变");
            assertEquals(2, store.size(), "初始2 +新增1 -删除1 = 当前 2 条");
        });

        tf.run("PageQuery 参数校验：非法 sort / pageSize 抛 400", () -> {
            try {
                PageQuery.fromParams(Map.of("sort", "weird"));
                fail("非法 sort 必须抛错");
            } catch (ApiException e) {
                assertEquals("invalid_sort", e.code(), "错误码应为 invalid_sort");
            }
            try {
                PageQuery.fromParams(Map.of("pageSize", "0"));
                fail("pageSize=0 必须抛错");
            } catch (ApiException e) {
                assertEquals("invalid_page_size", e.code(), "错误码应为 invalid_page_size");
            }
            try {
                PageQuery.fromParams(Map.of("pageSize", "101"));
                fail("pageSize=101 必须抛错");
            } catch (ApiException e) {
                assertEquals("invalid_page_size", e.code(), "错误码应为 invalid_page_size");
            }
        });
    }

    /** 可手动拨动的时钟，用于过期测试。 */
    static final class MutableClock extends Clock {
        private Instant now;

        MutableClock(Instant start) {
            this.now = start;
        }

        void advance(Duration d) {
            now = now.plus(d);
        }

        @Override
        public ZoneOffset getZone() {
            return ZoneOffset.UTC;
        }

        @Override
        public Clock withZone(java.time.ZoneId zone) {
            return this;
        }

        @Override
        public Instant instant() {
            return now;
        }
    }
}
