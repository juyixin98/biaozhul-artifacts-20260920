package vecsearch.index;

import vecsearch.core.DistanceMeter;
import vecsearch.core.SearchHit;
import vecsearch.core.TaggedVector;

import java.util.List;
import java.util.Map;

/**
 * 近似最近邻索引。
 *
 * <p>实现需要在向量写入/删除时被同步通知（{@link #upsert} / {@link #remove}），
 * 因此索引内容始终与存储一致；也可以随时 {@link #rebuild} 用全量数据重新训练。
 */
public interface SearchIndex {

    /** 索引名称，如 "ivf"。 */
    String name();

    /** 用全量数据（重新）训练并填充索引。 */
    void rebuild(StoreView view, IndexOptions options);

    /** 单条写入（已存在则替换）。 */
    void upsert(TaggedVector v);

    /** 删除一条，返回该 ID 是否在索引中。 */
    boolean remove(String id);

    /** 索引中现存向量数。 */
    int indexedCount();

    /** 聚类簇数量（0 表示空索引）。 */
    int clusterCount();

    /**
     * 近似检索。
     *
     * @param query  原始查询向量（余弦模式下非零，由调用方保证）
     * @param k      返回条数
     * @param filter 标签 AND 过滤，null/空表示不过滤
     * @param nprobe 搜索预算：探测最近的多少个簇
     * @param meter  距离计算计数器
     */
    List<SearchHit> search(float[] query, int k, Map<String, String> filter,
                           int nprobe, DistanceMeter meter);

    /** 供 /stats 展示的索引参数。 */
    Map<String, Object> describe();
}
