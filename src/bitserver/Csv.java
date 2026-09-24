package bitserver;

import java.util.ArrayList;
import java.util.List;

/**
 * 极简 CSV 读取器（RFC 4180 风格）：
 *  - 首行为表头，决定列名与列序；
 *  - 支持双引号包围字段、字段内逗号/换行/"" 转义；
 *  - 空行跳过（但引号内的空行属于字段，保留）；
 *  - 所有列都按“枚举（类别）列”处理，空字段统一为空字符串 ""。
 */
public final class Csv {

    private Csv() {
    }

    /** 解析结果：headers 为列名，rows 每行长度与 headers 一致。 */
    public static final class Table {
        public final List<String> headers;
        public final List<List<String>> rows;

        Table(List<String> headers, List<List<String>> rows) {
            this.headers = headers;
            this.rows = rows;
        }
    }

    public static Table parse(String content) {
        List<List<String>> records = new ArrayList<>();
        List<String> field = new ArrayList<>();
        StringBuilder cur = new StringBuilder();
        boolean inQuotes = false;
        boolean fieldStarted = false; // 区分“真正的空字段”与“行首”
        boolean anyContent = false;

        int i = 0;
        int len = content.length();
        while (i < len) {
            char c = content.charAt(i);
            if (inQuotes) {
                if (c == '"') {
                    if (i + 1 < len && content.charAt(i + 1) == '"') {
                        cur.append('"');
                        i += 2;
                    } else {
                        inQuotes = false;
                        i++;
                    }
                } else {
                    cur.append(c);
                    i++;
                }
            } else {
                switch (c) {
                    case '"':
                        inQuotes = true;
                        fieldStarted = true;
                        i++;
                        break;
                    case ',':
                        field.add(cur.toString());
                        cur.setLength(0);
                        fieldStarted = false;
                        anyContent = true;
                        i++;
                        break;
                    case '\r':
                        // 与 \n 配对处理 CRLF
                        if (i + 1 < len && content.charAt(i + 1) == '\n') {
                            i++;
                        }
                        i++;
                        field.add(cur.toString());
                        records.add(field);
                        field = new ArrayList<>();
                        cur.setLength(0);
                        fieldStarted = false;
                        anyContent = false;
                        break;
                    case '\n':
                        i++;
                        field.add(cur.toString());
                        records.add(field);
                        field = new ArrayList<>();
                        cur.setLength(0);
                        fieldStarted = false;
                        anyContent = false;
                        break;
                    default:
                        cur.append(c);
                        fieldStarted = true;
                        anyContent = true;
                        i++;
                }
            }
        }
        // 文件结尾最后一条记录（不带末尾换行，或处于引号内）
        if (inQuotes) {
            throw new IllegalArgumentException("CSV 解析失败：存在未闭合的引号");
        }
        if (fieldStarted || anyContent || !field.isEmpty()) {
            field.add(cur.toString());
            records.add(field);
        }

        if (records.isEmpty()) {
            throw new IllegalArgumentException("CSV 内容为空：至少需要一行表头");
        }

        List<String> headers = records.get(0);
        if (headers.isEmpty() || headers.stream().allMatch(String::isEmpty)) {
            throw new IllegalArgumentException("CSV 表头非法：没有任何列名");
        }
        for (int c = 0; c < headers.size(); c++) {
            if (headers.get(c).isEmpty()) {
                throw new IllegalArgumentException("CSV 表头非法：第 " + (c + 1) + " 列列名为空");
            }
            for (int c2 = 0; c2 < c; c2++) {
                if (headers.get(c2).equals(headers.get(c))) {
                    throw new IllegalArgumentException("CSV 表头非法：列名重复 '" + headers.get(c) + "'");
                }
            }
        }

        List<List<String>> rows = new ArrayList<>(records.size() - 1);
        for (int r = 1; r < records.size(); r++) {
            List<String> row = records.get(r);
            if (row.size() != headers.size()) {
                throw new IllegalArgumentException(
                        "CSV 第 " + (r + 1) + " 行有 " + row.size() + " 列，与表头 " + headers.size() + " 列不一致");
            }
            rows.add(row);
        }
        return new Table(headers, rows);
    }
}
