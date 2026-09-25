package dev.example.cp.tests;

import dev.example.cp.core.Event;
import dev.example.cp.core.KeyedSumOperator;
import dev.example.cp.core.OperatorSnapshot;

import java.util.LinkedHashMap;
import java.util.Map;

public class OperatorTest extends TestCase {

    public OperatorTest() {
        super("operator/keyed sum is deterministic and snapshot-restoreable");
    }

    @Override
    protected void run() {
        KeyedSumOperator op = new KeyedSumOperator();
        op.process(new Event(0, "a", 1));
        op.process(new Event(1, "b", 2));
        op.process(new Event(2, "a", 10));
        op.process(new Event(3, "b", 5));

        Map<String, Long> expect = new LinkedHashMap<>();
        expect.put("a", 11L);
        expect.put("b", 7L);
        assertEquals(expect, op.currentSums(), "sums after 4 events");

        // 快照 -> 全新算子恢复 -> 继续处理，结果必须等于不中断地处理完全部事件
        OperatorSnapshot snap = op.snapshot();
        KeyedSumOperator replayed = new KeyedSumOperator();
        replayed.restore(snap);
        replayed.process(new Event(4, "a", 100));
        KeyedSumOperator continuous = new KeyedSumOperator();
        continuous.process(new Event(0, "a", 1));
        continuous.process(new Event(1, "b", 2));
        continuous.process(new Event(2, "a", 10));
        continuous.process(new Event(3, "b", 5));
        continuous.process(new Event(4, "a", 100));
        assertEquals(continuous.currentSums(), replayed.currentSums(),
                "restore+continue must equal continuous execution");
    }
}
