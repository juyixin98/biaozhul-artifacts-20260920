package com.tvl.sql;

import java.util.List;

/**
 * SELECT 语句（受限子集）：
 *   SELECT 投影项 (, 投影项)* FROM 表名 [WHERE 谓词]
 */
public record Select(
        List<ProjectionItem> items,
        String table,
        Expr where // 可为 null
) {
    public boolean selectsAll() {
        return items.size() == 1 && "*".equals(items.get(0).name());
    }

    /** 投影项：表达式 + 输出列名（别名；无别名时用规范化的表达式文本）。 */
    public record ProjectionItem(Expr expr, String name) {
    }
}
