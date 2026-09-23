package tvl.core;

import java.util.LinkedHashMap;
import java.util.Map;

/**
 * 源码位置（均为 1 起始）：offset 为字符偏移，line/column 为行列号。
 * 错误信息中同时给出行列号，便于定位请求表达式中的出错点。
 */
public final class Pos {
    public final int offset;
    public final int line;
    public final int column;

    public Pos(int offset, int line, int column) {
        this.offset = offset;
        this.line = line;
        this.column = column;
    }

    public Map<String, Object> toJson() {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("offset", offset);
        m.put("line", line);
        m.put("column", column);
        return m;
    }

    @Override
    public String toString() {
        return line + ":" + column;
    }
}
