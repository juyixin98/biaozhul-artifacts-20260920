package dedup.state;

/** 快照存储抽象（便于测试内存实现，生产用文件实现）。 */
public interface SnapshotStore {

    /** 保存快照（必须原子：读到的快照要么是旧版本要么是新版本，不能是半截文件）。 */
    void save(SnapshotData snapshot) throws Exception;

    /** 读取最新快照；不存在时返回 null。 */
    SnapshotData load() throws Exception;

    /** 删除全部持久化状态（测试/admin reset 使用）。 */
    void clear() throws Exception;

    boolean exists() throws Exception;
}
