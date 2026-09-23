package phj.join;

/** 左连接中“探测行是否已匹配”的位图标记。 */
public interface ProbeMarker extends AutoCloseable {

    void mark(long index);

    boolean isMarked(long index);

    String kind();

    @Override
    default void close() { }
}
