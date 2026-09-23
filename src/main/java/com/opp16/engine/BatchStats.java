package com.opp16.engine;

import com.opp16.engine.json.Json;

import java.util.ArrayList;
import java.util.List;

/**
 * Per-batch bookkeeping emitted by the vectorised filter: which row range each
 * batch covered and how many survivors it produced. Lets the request response
 * and the tests prove that execution really crossed batch boundaries.
 */
public final class BatchStats {

    public static final class Batch {
        public final int index;
        public final int from;   // inclusive
        public final int to;     // exclusive
        public final int selected;

        Batch(int index, int from, int to, int selected) {
            this.index = index;
            this.from = from;
            this.to = to;
            this.selected = selected;
        }

        Json.Value toJson() {
            Json.Obj o = new Json.Obj();
            o.put("batch", index);
            o.put("from", from);
            o.put("to", to);
            o.put("selected", selected);
            return o;
        }
    }

    private final List<Batch> batches = new ArrayList<>();
    private int batchSize;

    public void setBatchSize(int n) { this.batchSize = n; }
    public int getBatchSize() { return batchSize; }

    /** Record a finished batch with its survivor count. */
    public void recordBatch(int index, int from, int to, int selected) {
        batches.add(new Batch(index, from, to, selected));
    }

    public List<Batch> batches() { return batches; }

    public Json.Value toJson() {
        Json.Obj o = new Json.Obj();
        o.put("batchSize", batchSize);
        o.put("batchCount", batches.size());
        Json.Arr arr = new Json.Arr();
        for (Batch b : batches) arr.add(b.toJson());
        o.put("batches", arr);
        return o;
    }
}
