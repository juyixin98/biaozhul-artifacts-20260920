package com.example.itasset.support;

import jakarta.persistence.EntityManager;
import jakarta.persistence.PersistenceContext;
import org.springframework.stereotype.Component;

import java.util.function.Supplier;

/**
 * MySQL named advisory locks ({@code GET_LOCK}). Unlike row locks these serialize the
 * <em>check-then-act</em> logic of month-end operations (posting vs. close vs.
 * adjustment) across transactions. The lock is released by {@code RELEASE_LOCK} in
 * {@code finally} (and auto-released when the connection returns to the pool).
 */
@Component
public class MysqlNamedLock {

    @PersistenceContext
    private EntityManager em;

    /** Runs the action while holding the named lock, waiting up to {@code timeoutSeconds}. */
    public <T> T withLock(String lockName, int timeoutSeconds, Supplier<T> action) {
        Number acquired = (Number) em.createNativeQuery("SELECT GET_LOCK(:name, :timeout)")
                .setParameter("name", lockName)
                .setParameter("timeout", timeoutSeconds)
                .getSingleResult();
        if (acquired == null || acquired.intValue() != 1) {
            throw new IllegalStateException("Could not acquire advisory lock: " + lockName);
        }
        try {
            return action.get();
        } finally {
            em.createNativeQuery("SELECT RELEASE_LOCK(:name)")
                    .setParameter("name", lockName)
                    .getSingleResult();
        }
    }
}
