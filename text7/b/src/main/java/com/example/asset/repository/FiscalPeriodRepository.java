package com.example.asset.repository;

import com.example.asset.domain.FiscalPeriod;
import jakarta.persistence.LockModeType;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Lock;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;

import java.util.Optional;

public interface FiscalPeriodRepository extends JpaRepository<FiscalPeriod, String> {

    /** 计提与关账共用同一把期间行锁，二者互斥。 */
    @Lock(LockModeType.PESSIMISTIC_WRITE)
    @Query("select p from FiscalPeriod p where p.period = :period")
    Optional<FiscalPeriod> findWithLockByPeriod(@Param("period") String period);
}
