package phj.join;

import phj.core.Row;
import phj.core.Value;
import phj.json.Json;

import java.util.ArrayList;
import java.util.List;

/**
 * 溢写文件的行编解码：一行一条 JSON 数组（JSON Lines）。
 * 例如 [1,"a",null,true,3.5]
 * 选用文本格式便于直接检查导出的分区文件。
 */
public final class RowCodec {

    private RowCodec() {}

    public static String encode(Row row) {
        List<Object> json = new ArrayList<>(row.width());
        for (int i = 0; i < row.width(); i++) json.add(row.get(i).toJson());
        return Json.write(json);
    }

    public static Row decode(String line) {
        List<Object> arr = Json.asArr(Json.parse(line), "溢写记录");
        List<Value> vals = new ArrayList<>(arr.size());
        for (Object o : arr) vals.add(Value.fromJson(o));
        return new Row(vals);
    }
}
