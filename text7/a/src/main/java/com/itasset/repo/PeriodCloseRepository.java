package com.itasset.repo;

import com.itasset.domain.PeriodClose;
import jakarta.persistence.LockModeType;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Lock;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;

import java.util.List;
import java.util.Optional;

public interface PeriodCloseRepository extends JpaRepository<PeriodClose, String> {

    /**
     * 对关账标记加行锁。行不存在时 InnoDB 加间隙锁：
     * 计提事务先锁本行（或占据间隙），关账事务的插入必须等待，
     * 反之亦然 —— 关账与计提因此完全串行。
     */
    @Lock(LockModeType.PESSIMISTIC_WRITE)
    @Query("select c from PeriodClose c where c.period = :period")
    Optional<PeriodClose> findByPeriodForUpdate(@Param("period") String period);

    /**
     * 期间 P 是否已冻结：存在关账行 C.period &gt;= P。
     * 按月顺序关账时即"P 已关账或早于最近关账期间"。
     */
    @Query("select count(c) > 0 from PeriodClose c where c.period >= :period")
    boolean isPeriodFrozen(@Param("period") String period);

    boolean existsByPeriod(String period);

    List<PeriodClose> findAllByOrderByPeriodDesc();
}
