package ij;

/** 一条输入事件（左流或右流通用）。payload 为不透明 JSON 节点，仅随配对结果回显。 */
final class Event {
    final String id;
    final String key;
    final long ts;
    final Object payload;

    Event(String id, String key, long ts, Object payload) {
        this.id = id;
        this.key = key;
        this.ts = ts;
        this.payload = payload;
    }
}
