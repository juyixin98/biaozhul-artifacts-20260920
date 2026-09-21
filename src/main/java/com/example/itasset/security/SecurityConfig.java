package com.example.itasset.security;

import jakarta.servlet.http.HttpServletResponse;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.security.config.annotation.method.configuration.EnableMethodSecurity;
import org.springframework.security.config.annotation.web.builders.HttpSecurity;
import org.springframework.security.config.http.SessionCreationPolicy;
import org.springframework.security.crypto.bcrypt.BCryptPasswordEncoder;
import org.springframework.security.crypto.password.PasswordEncoder;
import org.springframework.security.web.AuthenticationEntryPoint;
import org.springframework.security.web.SecurityFilterChain;
import org.springframework.security.web.access.AccessDeniedHandler;

/**
 * 角色矩阵（在 Controller 上以 @PreAuthorize 强制执行，此处兜底）：
 * <ul>
 *   <li>FINANCE 财务：关账/重开查询、参数调整、导出。</li>
 *   <li>MANAGER 资产经理：建档、状态转换、计提。</li>
 *   <li>VIEWER 只读：全部 GET。</li>
 * </ul>
 */
@Configuration
@EnableMethodSecurity
public class SecurityConfig {

    @Bean
    public PasswordEncoder passwordEncoder() {
        return new BCryptPasswordEncoder(10);
    }

    @Bean
    public SecurityFilterChain filterChain(HttpSecurity http) throws Exception {
        http
                .csrf(csrf -> csrf.disable())
                .sessionManagement(sm -> sm.sessionCreationPolicy(SessionCreationPolicy.STATELESS))
                .authorizeHttpRequests(auth -> auth
                        .requestMatchers(
                                "/v3/api-docs/**",
                                "/swagger-ui/**",
                                "/swagger-ui.html",
                                "/actuator/health"
                        ).permitAll()
                        .anyRequest().authenticated())
                .httpBasic(basic -> {
                })
                .exceptionHandling(eh -> eh
                        .authenticationEntryPoint(restAuthEntryPoint())
                        .accessDeniedHandler(restDeniedHandler()));
        return http.build();
    }

    private AuthenticationEntryPoint restAuthEntryPoint() {
        return (request, response, authException) -> {
            response.setStatus(HttpServletResponse.SC_UNAUTHORIZED);
            response.setContentType("application/json;charset=UTF-8");
            response.getWriter().write("{\"status\":401,\"code\":\"UNAUTHORIZED\","
                    + "\"message\":\"需要 HTTP Basic 认证（finance/manager/viewer）\"}");
        };
    }

    private AccessDeniedHandler restDeniedHandler() {
        return (request, response, accessDeniedException) -> {
            response.setStatus(HttpServletResponse.SC_FORBIDDEN);
            response.setContentType("application/json;charset=UTF-8");
            response.getWriter().write("{\"status\":403,\"code\":\"FORBIDDEN\","
                    + "\"message\":\"当前角色无权执行该操作\"}");
        };
    }
}
