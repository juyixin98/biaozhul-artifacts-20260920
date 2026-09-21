package com.example.itasset.support;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

import java.time.Clock;
import java.time.LocalDate;
import java.time.ZoneId;

/**
 * The accounting clock. Normally the real system clock; tests (and disaster-recovery
 * reruns) can pin it with {@code app.clock.fixed=YYYY-MM-DD}.
 */
@Configuration
public class ClockConfig {

    @Bean
    public Clock accountingClock(@Value("${app.clock.fixed:}") String fixed,
                                 @Value("${app.clock.zone:Asia/Shanghai}") String zone) {
        ZoneId zoneId = ZoneId.of(zone);
        if (fixed == null || fixed.isBlank()) {
            return Clock.system(zoneId);
        }
        return Clock.fixed(LocalDate.parse(fixed).atStartOfDay(zoneId).toInstant(), zoneId);
    }
}
