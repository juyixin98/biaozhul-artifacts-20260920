package com.itasset.config;

import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.security.config.annotation.web.builders.HttpSecurity;
import org.springframework.security.config.annotation.web.configurers.AbstractHttpConfigurer;
import org.springframework.security.core.userdetails.User;
import org.springframework.security.core.userdetails.UserDetailsService;
import org.springframework.security.crypto.factory.PasswordEncoderFactories;
import org.springframework.security.crypto.password.PasswordEncoder;
import org.springframework.security.provisioning.InMemoryUserDetailsManager;
import org.springframework.security.web.SecurityFilterChain;

/**
 * HTTP Basic 三角色：
 * <ul>
 *   <li>FINANCE（finance/finance123）：期间关账、参数调整；</li>
 *   <li>ASSET_MANAGER（manager/manager123）：资产维护、状态转换、触发折旧；</li>
 *   <li>VIEWER（viewer/viewer123）：只读；</li>
 *   <li>折旧计提放宽给 ASSET_MANAGER 与 FINANCE（月末跑批通常由财务发起）。</li>
 * </ul>
 */
@Configuration
public class SecurityConfig {

    public static final String ROLE_FINANCE = "FINANCE";
    public static final String ROLE_MANAGER = "ASSET_MANAGER";
    public static final String ROLE_VIEWER = "VIEWER";

    @Bean
    public SecurityFilterChain filterChain(HttpSecurity http) throws Exception {
        http
                .csrf(AbstractHttpConfigurer::disable)
                .authorizeHttpRequests(auth -> auth
                        .requestMatchers("/actuator/health", "/error").permitAll()
                        // 只读查询：三种角色均可
                        .requestMatchers(org.springframework.http.HttpMethod.GET,
                                "/api/assets/**", "/api/periods/**")
                        .hasAnyRole(ROLE_FINANCE, ROLE_MANAGER, ROLE_VIEWER)
                        // 资产维护、状态转换：资产经理
                        .requestMatchers(org.springframework.http.HttpMethod.POST, "/api/assets")
                        .hasRole(ROLE_MANAGER)
                        .requestMatchers("/api/assets/*/transitions").hasRole(ROLE_MANAGER)
                        // 折旧跑批：经理或财务
                        .requestMatchers("/api/assets/*/depreciation/runs")
                        .hasAnyRole(ROLE_MANAGER, ROLE_FINANCE)
                        // 关账、参数调整：财务
                        .requestMatchers(org.springframework.http.HttpMethod.POST, "/api/periods/*/close")
                        .hasRole(ROLE_FINANCE)
                        .requestMatchers("/api/assets/*/adjustments").hasRole(ROLE_FINANCE)
                        .anyRequest().authenticated())
                .httpBasic(b -> {
                });
        return http.build();
    }

    @Bean
    public UserDetailsService userDetailsService(PasswordEncoder encoder) {
        return new InMemoryUserDetailsManager(
                User.withUsername("finance").password(encoder.encode("finance123"))
                        .roles(ROLE_FINANCE).build(),
                User.withUsername("manager").password(encoder.encode("manager123"))
                        .roles(ROLE_MANAGER).build(),
                User.withUsername("viewer").password(encoder.encode("viewer123"))
                        .roles(ROLE_VIEWER).build());
    }

    @Bean
    public PasswordEncoder passwordEncoder() {
        return PasswordEncoderFactories.createDelegatingPasswordEncoder();
    }
}
