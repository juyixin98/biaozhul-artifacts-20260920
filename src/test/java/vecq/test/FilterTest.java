package vecq.test;

import java.util.List;

/**
 * 过滤测试：整型 / 字符串比较、IS [NOT] NULL、AND / OR / NOT、普通 OR 的去重语义、
 * 以及向量化引擎与逐行解释器结果一致（差分在 QueryEngine 内部已强制校验）。
 *
 * orders 表：id=[10,20,30,NULL,50,60]，status=[NEW,PAID,NEW,NULL,PAID,NEW]
 */
public final class FilterTest {

    private static final String TABLE = """
            "table":{"name":"orders","columns":[
              {"name":"id","type":"int","values":[10,20,30,null,50,60]},
              {"name":"status","type":"string","values":["NEW","PAID","NEW",null,"PAID","NEW"]}
            ]}""";

    private static String req(String filter, Integer batchSize) {
        String bs = batchSize == null ? "" : (",\"batchSize\":" + batchSize);
        return "{" + TABLE + "," + filter + bs + "}";
    }

    public static void run() {
        // 整型比较
        Assert.eq(List.of(0, 1, 2), Q.rows(req("\"filter\":{\"column\":\"id\",\"op\":\"<\",\"value\":50}", 4)),
                "id < 50 命中前 3 行（NULL 不命中）");
        Assert.eq(List.of(4, 5), Q.rows(req("\"filter\":{\"column\":\"id\",\"op\":\">=\",\"value\":50}", 2)),
                "id >= 50 命中 4,5（batchSize=2 跨越 3 个批边界）");
        Assert.eq(List.of(1), Q.rows(req("\"filter\":{\"column\":\"id\",\"op\":\"=\",\"value\":20}", 1)),
                "id = 20（batchSize=1，每行一批）");
        Assert.eq(java.util.Arrays.asList(0, 2, 4, 5),
                Q.rows(req("\"filter\":{\"column\":\"id\",\"op\":\"!=\",\"value\":20}", 4)),
                "id != 20：命中除 20 行与 NULL 行以外的全部行");

        // 字符串比较
        Assert.eq(List.of(0, 2, 5),
                Q.rows(req("\"filter\":{\"column\":\"status\",\"op\":\"=\",\"value\":\"NEW\"}", 4)),
                "status = 'NEW'");
        Assert.eq(List.of(1, 4),
                Q.rows(req("\"filter\":{\"column\":\"status\",\"op\":\"!=\",\"value\":\"NEW\"}", 4)),
                "status != 'NEW'：NULL 行被排除");

        // IS NULL / IS NOT NULL
        Assert.eq(List.of(3),
                Q.rows(req("\"filter\":{\"op\":\"isNull\",\"column\":\"id\"}", 4)),
                "id IS NULL");
        Assert.eq(List.of(0, 1, 2, 4, 5),
                Q.rows(req("\"filter\":{\"op\":\"isNotNull\",\"column\":\"id\"}", 4)),
                "id IS NOT NULL");

        // AND / OR / NOT
        Assert.eq(List.of(0, 2),
                Q.rows(req("""
                        "filter":{"op":"and","children":[
                          {"column":"status","op":"=","value":"NEW"},
                          {"column":"id","op":"<","value":50}
                        ]}""", 2)),
                "status=NEW AND id<50");
        Assert.eq(List.of(2, 4),
                Q.rows(req("""
                        "filter":{"op":"or","children":[
                          {"column":"id","op":"=","value":30},
                          {"column":"id","op":"=","value":50}
                        ]}""", 3)),
                "id=30 OR id=50（普通 OR 每行至多一次）");
        // NOT：NOT(status=NEW)，NULL 行谓词为 UNKNOWN，NOT UNKNOWN 仍是 UNKNOWN -> 排除
        Assert.eq(List.of(1, 4),
                Q.rows(req("""
                        "filter":{"op":"not","child":{"column":"status","op":"=","value":"NEW"}}
                        """, 4)),
                "NOT(status=NEW)：命中两个 PAID，NULL 行保持 UNKNOWN 被排除");

        // 与 NULL 字面量比较恒 UNKNOWN -> 无命中
        Assert.eq(List.of(),
                Q.rows(req("\"filter\":{\"column\":\"id\",\"op\":\"=\",\"value\":null}", 4)),
                "id = NULL 不命中任何行（3VL）");

        // 无过滤 = 全选
        Assert.eq(List.of(0, 1, 2, 3, 4, 5),
                Q.rows("{" + TABLE + ",\"batchSize\":4}"),
                "无 filter 时全选（含 NULL 行）");
    }
}
