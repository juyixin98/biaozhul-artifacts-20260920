package com.example.itasset.repo;

import com.example.itasset.domain.AccountingPeriod;
import org.springframework.data.jpa.repository.JpaRepository;

import java.util.Optional;

public interface AccountingPeriodRepository extends JpaRepository<AccountingPeriod, Long> {

    Optional<AccountingPeriod> findByPeriod(Integer period);

    boolean existsByPeriod(Integer period);
}
