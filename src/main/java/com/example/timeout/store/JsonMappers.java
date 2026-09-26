package com.example.timeout.store;

import com.fasterxml.jackson.databind.DeserializationFeature;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.SerializationFeature;
import com.fasterxml.jackson.datatype.jsr310.JavaTimeModule;

/** 全项目共用的 Jackson 配置：ISO-8601 日期时间、拒绝未知属性（输入在边界处校验）。 */
public final class JsonMappers {

    private JsonMappers() {
    }

    public static ObjectMapper mapper() {
        return new ObjectMapper()
                .registerModule(new JavaTimeModule())
                // 日期时间一律以 ISO-8601 文本输出（如 2026-09-25T11:00:00Z），不写数字时间戳
                .disable(SerializationFeature.WRITE_DATES_AS_TIMESTAMPS)
                // 边界校验：未知字段直接报错，避免悄悄吞掉调用方的拼写错误
                .configure(DeserializationFeature.FAIL_ON_UNKNOWN_PROPERTIES, true);
    }
}
