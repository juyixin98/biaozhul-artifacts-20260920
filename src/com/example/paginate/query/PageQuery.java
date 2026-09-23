package com.example.paginate.query;

import com.example.paginate.model.Item;
import com.example.paginate.web.ApiException;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.Comparator;
import java.util.HexFormat;
import java.util.Map;

/**
 * 一次列表查询的完整定义（游标与此绑定，换任何一个筛选/排序/页大小参数都不能复用游标）。
 *
 * 支持参数：
 *   sort     = name_asc | name_desc | score_asc | score_desc（默认 name_asc）
 *   category = 精确筛选（可空）
 *   q        = name 包含子串（大小写不敏感，可空）
 *   pageSize = 1..100（默认 10）
 *
 * 稳定次序：(排序键, id) 复合全序，排序键重复时由唯一 id 决胜。
 */
public record PageQuery(
        String sort,
        String category,
        String q,
        int pageSize
) {

    public static final int DEFAULT_PAGE_SIZE = 10;
    public static final int MAX_PAGE_SIZE = 100;

    public static PageQuery fromParams(Map<String, String> params) {
        String sort = params.getOrDefault("sort", "name_asc");
        if (!sort.equals("name_asc") && !sort.equals("name_desc")
                && !sort.equals("score_asc") && !sort.equals("score_desc")) {
            throw ApiException.badRequest("invalid_sort",
                    "sort 必须是 name_asc|name_desc|score_asc|score_desc，实际为: " + sort);
        }
        String category = emptyToNull(params.get("category"));
        String q = emptyToNull(params.get("q"));

        int pageSize = DEFAULT_PAGE_SIZE;
        String pageSizeRaw = params.get("pageSize");
        if (pageSizeRaw != null && !pageSizeRaw.isEmpty()) {
            try {
                pageSize = Integer.parseInt(pageSizeRaw);
            } catch (NumberFormatException e) {
                throw ApiException.badRequest("invalid_page_size",
                        "pageSize 必须是 1.." + MAX_PAGE_SIZE + " 的整数，实际为: " + pageSizeRaw);
            }
            if (pageSize < 1 || pageSize > MAX_PAGE_SIZE) {
                throw ApiException.badRequest("invalid_page_size",
                        "pageSize 必须在 1.." + MAX_PAGE_SIZE + " 之间，实际为: " + pageSize);
            }
        }
        return new PageQuery(sort, category, q, pageSize);
    }

    /**
     * 查询指纹：参与游标绑定。游标里的 queryHash 必须与当前请求一致，
     * 否则拒绝（换筛选/排序/页大小不能复用游标）。
     */
    public String queryHash() {
        String canonical = sort + "|" + nullDash(category) + "|" + nullDash(q) + "|" + pageSize;
        try {
            MessageDigest md = MessageDigest.getInstance("SHA-256");
            byte[] digest = md.digest(canonical.getBytes(StandardCharsets.UTF_8));
            return HexFormat.of().formatHex(digest).substring(0, 32);
        } catch (NoSuchAlgorithmException e) {
            throw new IllegalStateException(e);
        }
    }

    public boolean matches(Item item) {
        if (category != null && !category.equals(item.category())) {
            return false;
        }
        if (q != null && !item.name().toLowerCase().contains(q.toLowerCase())) {
            return false;
        }
        return true;
    }

    /** (排序键, id) 复合全序比较器，asc 表示整体从小到大。 */
    public Comparator<Item> comparator() {
        return switch (sort) {
            case "name_asc" -> Comparator.comparing(Item::name).thenComparingLong(Item::id);
            case "name_desc" -> Comparator.comparing(Item::name, Comparator.reverseOrder())
                    .thenComparingLong(Item::id);
            case "score_asc" -> Comparator.comparingLong(Item::score).thenComparingLong(Item::id);
            case "score_desc" -> Comparator.comparingLong(Item::score).reversed()
                    .thenComparingLong(Item::id);
            default -> throw new IllegalStateException("未知 sort: " + sort);
        };
    }

    /**
     * 游标推进比较：item 是否严格排在锚点（游标记录的最后一条）之后。
     * 必须与 {@link #comparator()} 的全序完全一致。
     */
    public boolean strictlyAfter(Item item, String anchorSortValue, long anchorId) {
        return compareAnchor(item, anchorSortValue, anchorId) > 0;
    }

    private int compareAnchor(Item item, String anchorSortValue, long anchorId) {
        return switch (sort) {
            case "name_asc", "name_desc" -> compareName(item, anchorSortValue, anchorId);
            case "score_asc", "score_desc" -> compareScore(item, anchorSortValue, anchorId);
            default -> throw new IllegalStateException("未知 sort: " + sort);
        };
    }

    private int compareName(Item item, String anchorName, long anchorId) {
        int c = item.name().compareTo(anchorName);
        if (c != 0) {
            return nameAscending() ? c : -c;
        }
        return Long.compare(item.id(), anchorId);
    }

    private int compareScore(Item item, String anchorScoreRaw, long anchorId) {
        long anchorScore;
        try {
            anchorScore = Long.parseLong(anchorScoreRaw);
        } catch (NumberFormatException e) {
            throw ApiException.badRequest("cursor_invalid", "游标中的排序值无法解析");
        }
        int c = Long.compare(item.score(), anchorScore);
        if (c != 0) {
            return scoreAscending() ? c : -c;
        }
        return Long.compare(item.id(), anchorId);
    }

    public boolean nameAscending() {
        return sort.equals("name_asc");
    }

    public boolean scoreAscending() {
        return sort.equals("score_asc");
    }

    /** 把某条记录的排序键转成游标字符串（score 用十进制，name 用原文）。 */
    public String sortValueOf(Item item) {
        return sort.startsWith("score") ? Long.toString(item.score()) : item.name();
    }

    private static String emptyToNull(String s) {
        return (s == null || s.isEmpty()) ? null : s;
    }

    private static String nullDash(String s) {
        return s == null ? "-" : s;
    }
}
