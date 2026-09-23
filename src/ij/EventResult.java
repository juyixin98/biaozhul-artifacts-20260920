package ij;

import java.util.ArrayList;
import java.util.List;

/** 单条事件接入的处理结果：新产生的配对 + 是否被判定为迟到丢弃。 */
final class EventResult {
    final String eventId;
    final boolean dropped;
    final List<Pair> newPairs = new ArrayList<>();

    EventResult(String eventId, boolean dropped) {
        this.eventId = eventId;
        this.dropped = dropped;
    }
}
