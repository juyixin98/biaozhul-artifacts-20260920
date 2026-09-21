package com.example.itasset;

import io.swagger.v3.oas.models.OpenAPI;
import io.swagger.v3.oas.models.info.Info;
import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.context.annotation.Bean;

import java.time.Clock;

@SpringBootApplication
public class ItAssetApplication {

    public static void main(String[] args) {
        SpringApplication.run(ItAssetApplication.class, args);
    }

    /** 注入 Clock 便于测试固定“当前时间”。 */
    @Bean
    public Clock clock() {
        return Clock.systemDefaultZone();
    }

    @Bean
    public OpenAPI openAPI() {
        return new OpenAPI().info(new Info()
                .title("企业 IT 资产生命周期 API")
                .version("1.0.0")
                .description("状态转换、直线/余额递减折旧、可追溯调整。HTTP Basic 鉴权。"));
    }
}
