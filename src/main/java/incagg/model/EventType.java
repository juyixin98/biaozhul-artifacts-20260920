package incagg.model;

/**
 * 业务事件类型。双边（订单行 / 商品维表）各自支持插入、更新、删除。
 */
public enum EventType {
    ORDER_UPSERT,
    ORDER_DELETE,
    PRODUCT_UPSERT,
    PRODUCT_DELETE
}
