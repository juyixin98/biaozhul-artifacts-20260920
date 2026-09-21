package com.example.itasset.security;

import com.example.itasset.repo.AppUserRepository;
import org.springframework.security.core.authority.SimpleGrantedAuthority;
import org.springframework.security.core.userdetails.User;
import org.springframework.security.core.userdetails.UserDetails;
import org.springframework.security.core.userdetails.UserDetailsService;
import org.springframework.security.core.userdetails.UsernameNotFoundException;
import org.springframework.stereotype.Service;

import java.util.List;

@Service
public class DbUserDetailsService implements UserDetailsService {

    private final AppUserRepository users;

    public DbUserDetailsService(AppUserRepository users) {
        this.users = users;
    }

    @Override
    public UserDetails loadUserByUsername(String username) throws UsernameNotFoundException {
        return users.findByUsername(username)
                .map(u -> new User(u.getUsername(), u.getPasswordHash(), u.isEnabled(), true, true, true,
                        List.of(new SimpleGrantedAuthority("ROLE_" + u.getRole()))))
                .orElseThrow(() -> new UsernameNotFoundException("Unknown user: " + username));
    }
}
