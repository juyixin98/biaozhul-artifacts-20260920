package com.tvl.engine;

import com.tvl.columnar.Batch;
import com.tvl.core.TruthVector;

import java.util.List;

/**
 * 一个输入批次对应的执行结果。
 * truth 为 WHERE 的原始三值向量（含 UNKNOWN），output 为过滤后的投影批次。
 */
public record BatchResult(
        int inputRows,
        TruthVector truth,
        Batch output,
        String crossCheck // "MATCH" / "MISMATCH: ..."，未对照时为 null
) {
}
