package com.example.itasset.bootstrap;

import com.example.itasset.domain.UserRole;
import com.example.itasset.repo.AppUserRepository;
import com.example.itasset.domain.AppUser;
import org.springframework.boot.CommandLineRunner;
import org.springframework.security.crypto.password.PasswordEncoder;
import org.springframework.stereotype.Component;

import java.time.Clock;
import java.time.Instant;
import java.util.List;

/**
 * 启动时幂等写入三个内置用户，密码在此处经 BCrypt 编码，避免迁移脚本内固化不可校验的哈希。
 * 生产环境应通过独立用户管理流程维护，样例仅为演示。
 */
@Component
public class DefaultUsersSeeder implements CommandLineRunner {

    public record DefaultUser(String username, String rawPassword, UserRole role) {
    }

    public static final List<DefaultUser> DEFAULT_USERS = List.of(
            new DefaultUser("finance", "Finance#2026", UserRole.FINANCE),
            new DefaultUser("manager", "Manager#2026", UserRole.MANAGER),
            new DefaultUser("viewer", "Viewer#2026", UserRole.VIEWER)
    );

    private final AppUserRepository users;
    private final PasswordEncoder encoder;
    private final Clock clock;

    public DefaultUsersSeeder(AppUserRepository users, PasswordEncoder encoder, Clock clock) {
        this.users = users;
        this.encoder = encoder;
        this.clock = clock;
    }

    @Override
    public void run(String... args) {
        Instant now = Instant.now(clock);
        for (DefaultUser u : DEFAULT_USERS) {
            if (!users.existsByUsername(u.username())) {
                users.save(new AppUser(u.username(), encoder.encode(u.rawPassword()), u.role(), now));
            }
        }
    }
}
