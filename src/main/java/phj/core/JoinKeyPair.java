package phj.core;

import java.util.ArrayList;
import java.util.List;

/** 一对连接键（左右列名按位置配对）。 */
public record JoinKeyPair(String leftColumn, String rightColumn) {

    /**
     * 解析请求中的 keys 字段。支持两种写法：
     *  - 简写：{"keys":["k1","k2"]}            左右同名列；
     *  - 配对：{"keys":[["la","ra"],["lb","rb"]]}
     */
    public static List<JoinKeyPair> parseKeys(Object keysObj) {
        if (keysObj == null) throw new IllegalArgumentException("缺少连接键字段 'keys'（至少一列）");
        List<Object> arr = phj.json.Json.asArr(keysObj, "keys");
        if (arr.isEmpty()) throw new IllegalArgumentException("连接键至少需要一列");
        List<JoinKeyPair> pairs = new ArrayList<>();
        for (Object o : arr) {
            if (o instanceof List<?> pair) {
                if (pair.size() != 2) {
                    throw new IllegalArgumentException("连接键对必须是 [左列, 右列] 两元素数组");
                }
                pairs.add(new JoinKeyPair(
                        phj.json.Json.asStr(pair.get(0), "连接键左列名"),
                        phj.json.Json.asStr(pair.get(1), "连接键右列名")));
            } else if (o instanceof String s) {
                pairs.add(new JoinKeyPair(s, s));
            } else {
                throw new IllegalArgumentException("连接键必须是字符串或 [左列,右列] 数组");
            }
        }
        return pairs;
    }
}
