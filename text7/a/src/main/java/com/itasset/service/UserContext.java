package com.itasset.service;

/**
 * 当前操作人（来自 HTTP Basic 登录用户），供服务层在仅追加记录上留痕。
 * 由 web 层拦截器在请求开始时设置、结束时清理。
 */
public final class UserContext {

    private static final ThreadLocal<String> CURRENT = new ThreadLocal<>();

    private UserContext() {
    }

    public static void set(String user) {
        CURRENT.set(user);
    }

    public static String currentUser() {
        String u = CURRENT.get();
        return u == null ? "system" : u;
    }

    public static void clear() {
        CURRENT.remove();
    }
}
