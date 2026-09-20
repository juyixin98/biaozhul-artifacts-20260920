package com.itasset.repo;

import com.itasset.domain.PeriodMutex;
import jakarta.persistence.LockModeType;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Lock;
import org.springframework.data.jpa.repository.Modifying;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;

import java.util.Optional;

public interface PeriodMutexRepository extends JpaRepository<PeriodMutex, String> {

    /** 幂等插入互斥量行（MySQL INSERT IGNORE），不与已持锁事务冲突。 */
    @Modifying
    @Query(value = "INSERT IGNORE INTO period_mutex(period) VALUES (:period)", nativeQuery = true)
    int insertIgnore(@Param("period") String period);

    @Lock(LockModeType.PESSIMISTIC_WRITE)
    @Query("select m from PeriodMutex m where m.period = :period")
    Optional<PeriodMutex> lock(@Param("period") String period);
}
