package vecsearch.index;

import vecsearch.core.Metric;
import vecsearch.core.SearchHit;

import java.util.List;
import java.util.Map;

/**
 * 索引构建时看到的只读数据视图（VectorStore 在读锁内提供），
 * 使索引不直接依赖可变的存储结构。
 */
public interface StoreView {

    record Item(String id, float[] vector, Map<String, String> filter) {
    }

    /** 当时所有存活向量（顺序稳定：按插入顺序）。 */
    List<Item> items();

    int dimension();

    Metric metric();
}
