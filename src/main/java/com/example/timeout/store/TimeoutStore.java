package com.example.timeout.store;

import com.example.timeout.core.RegistrySnapshot;
import com.example.timeout.core.TimeoutEntry;

import java.util.List;

/** 超时存储抽象。实现方只能持久化墙钟截止时间，单调读数一律不得落盘。 */
public interface TimeoutStore {

    void persist(RegistrySnapshot snapshot) throws StoreException;

    /** 返回上次持久化的条目（仅墙钟字段可信）；文件不存在时返回空列表。 */
    List<TimeoutEntry> load() throws StoreException;
}
