package com.itasset.service;

/**
 * 冲突（409）：乐观锁版本不符、非法状态转换、重复 requestId 语义不一致、
 * 期间已关账、并发竞争落败等。
 */
public class ConflictException extends RuntimeException {
    public ConflictException(String message) {
        super(message);
    }
}
