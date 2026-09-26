package bitemporal.json;

import com.fasterxml.jackson.databind.DeserializationFeature;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.SerializationFeature;
import com.fasterxml.jackson.datatype.jsr310.JavaTimeModule;

/** 全局唯一的 Jackson 配置：Instant 序列化为 ISO-8601 字符串，不用时间戳数字。 */
public final class JsonMapper {

    private static final ObjectMapper MAPPER = create();

    private JsonMapper() {
    }

    public static ObjectMapper get() {
        return MAPPER;
    }

    private static ObjectMapper create() {
        ObjectMapper mapper = new ObjectMapper();
        mapper.registerModule(new JavaTimeModule());
        mapper.disable(SerializationFeature.WRITE_DATES_AS_TIMESTAMPS);
        mapper.disable(DeserializationFeature.FAIL_ON_UNKNOWN_PROPERTIES);
        return mapper;
    }
}
