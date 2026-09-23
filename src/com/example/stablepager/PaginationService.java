package com.example.stablepager;

import java.util.ArrayList;
import java.util.Comparator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Keyset pagination over immutable snapshots.
 *
 * <p>The sort order is the total order {@code (sortField, id)}: the user-supplied
 * sort key first (ascending or descending) and, when keys repeat, the globally
 * unique row id ascending as the final tie-breaker. A page resumes strictly
 * after the last returned tuple, so duplicate sort keys can never cause rows to
 * be skipped or repeated.
 */
public final class PaginationService {

    private final MvccStore store;
    private final CursorCodec codec;

    public PaginationService(MvccStore store, CursorCodec codec) {
        this.store = store;
        this.codec = codec;
    }

    public Map<String, Object> list(QuerySpec query, String cursorToken) {
        final MvccStore.Snapshot snapshot;
        Item afterRow = null; // resume strictly after this row's tuple (null = first page)

        if (cursorToken == null) {
            snapshot = store.createSnapshot();
        } else {
            Map<String, Object> payload = codec.decode(cursorToken); // throws CURSOR_INVALID
            String fingerprint = (String) payload.get("f");
            if (!query.fingerprint().equals(fingerprint)) {
                throw new ApiException(400, "QUERY_MISMATCH",
                        "this cursor was issued for a different query; restart from the first page "
                                + "without a cursor or re-run the original query")
                        .with("cursorQuery", fingerprint)
                        .with("requestQuery", query.fingerprint());
            }
            snapshot = store.resolve((String) payload.get("s")); // throws SNAPSHOT_EXPIRED
            Number payloadVersion = (Number) payload.get("v");
            if (payloadVersion == null || payloadVersion.longValue() != snapshot.version()) {
                throw new ApiException(400, "CURSOR_INVALID", "invalid cursor: snapshot version mismatch");
            }
            String lastId = (String) payload.get("id");
            afterRow = snapshot.find(lastId).orElseThrow(() ->
                    new ApiException(400, "CURSOR_INVALID",
                            "invalid cursor: referenced row no longer belongs to the snapshot"));
            Object storedKey = payload.get("k");
            if (!keyMatches(query.sortValue(afterRow), storedKey)) {
                throw new ApiException(400, "CURSOR_INVALID", "invalid cursor: last sort tuple mismatch");
            }
        }

        // Filter then sort the immutable snapshot view.
        Comparator<Item> comparator = tupleComparator(query);
        List<Item> all = new ArrayList<>(snapshot.rows().size());
        for (Item item : snapshot.rows()) {
            if (query.matches(item)) {
                all.add(item);
            }
        }
        all.sort(comparator);

        int start = 0;
        if (afterRow != null) {
            // Locate the resume position. The row always exists inside the
            // snapshot, so binary search on the total order is exact.
            int idx = binarySearch(all, afterRow, comparator);
            if (idx < 0) {
                // Row exists in snapshot but not in this filter result: keep
                // walking forward to the first tuple strictly after it.
                idx = -idx - 1;
            } else {
                idx = idx + 1; // strictly after
            }
            start = idx;
        }

        int end = Math.min(start + query.limit(), all.size());
        List<Item> page = start >= all.size() ? List.of() : new ArrayList<>(all.subList(start, end));
        boolean hasMore = end < all.size();

        String nextCursor = null;
        if (hasMore) {
            nextCursor = codec.encode(buildPayload(query, snapshot, page.get(page.size() - 1)));
        }

        Map<String, Object> body = new LinkedHashMap<>();
        List<Object> itemsJson = new ArrayList<>(page.size());
        for (Item item : page) {
            itemsJson.add(item.toJson());
        }
        body.put("items", itemsJson);
        body.put("nextCursor", nextCursor);

        Map<String, Object> meta = new LinkedHashMap<>();
        meta.put("snapshotId", snapshot.id());
        meta.put("snapshotVersion", snapshot.version());
        meta.put("snapshotCreatedAt", snapshot.createdAt());
        meta.put("snapshotExpiresAt", snapshot.createdAt() + store.ttlMillis());
        meta.put("snapshotTtlMillis", store.ttlMillis());
        meta.put("sort", query.sortField());
        meta.put("order", query.ascending() ? "asc" : "desc");
        meta.put("limit", query.limit());
        meta.put("count", page.size());
        meta.put("hasMore", hasMore);
        Map<String, Object> filters = new LinkedHashMap<>();
        filters.put("nameContains", query.nameContains());
        filters.put("category", query.category());
        meta.put("filters", filters);
        body.put("page", meta);
        return body;
    }

    private static Map<String, Object> buildPayload(QuerySpec query, MvccStore.Snapshot snapshot, Item last) {
        Map<String, Object> payload = new LinkedHashMap<>();
        payload.put("f", query.fingerprint());
        payload.put("s", snapshot.id());
        payload.put("v", snapshot.version());
        payload.put("k", query.sortValue(last));
        payload.put("id", last.id());
        return payload;
    }

    private static Comparator<Item> tupleComparator(QuerySpec query) {
        return (a, b) -> {
            int c = compareComparables(query.sortValue(a), query.sortValue(b));
            if (!query.ascending()) {
                c = -c;
            }
            if (c != 0) {
                return c;
            }
            // Unique id is the stable final tie-breaker, always ascending,
            // independent of the requested direction.
            return a.id().compareTo(b.id());
        };
    }

    @SuppressWarnings({"rawtypes", "unchecked"})
    private static int compareComparables(Comparable<?> a, Comparable<?> b) {
        if (a.getClass() != b.getClass()) {
            // Cannot happen for a fixed sort field, but stay total-order safe.
            return Integer.compare(a.getClass().getName().hashCode(), b.getClass().getName().hashCode());
        }
        return ((Comparable) a).compareTo(b);
    }

    /**
     * Checks the unsigned key embedded in the cursor against the row's current
     * (snapshot) sort value. Numbers are compared by numeric value (JSON parsing
     * may yield Long/Integer), strings exactly.
     */
    private static boolean keyMatches(Comparable<?> expected, Object stored) {
        if (expected instanceof Long l) {
            return stored instanceof Number n && n.longValue() == l;
        }
        return expected.equals(stored);
    }

    private static int binarySearch(List<Item> list, Item key, Comparator<Item> cmp) {
        int lo = 0;
        int hi = list.size() - 1;
        while (lo <= hi) {
            int mid = (lo + hi) >>> 1;
            int c = cmp.compare(list.get(mid), key);
            if (c < 0) {
                lo = mid + 1;
            } else if (c > 0) {
                hi = mid - 1;
            } else {
                return mid;
            }
        }
        return -(lo + 1);
    }
}
