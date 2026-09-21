package com.example.itasset.repo;

import com.example.itasset.domain.DepreciationRun;
import org.springframework.data.jpa.repository.JpaRepository;

import java.util.Optional;

public interface DepreciationRunRepository extends JpaRepository<DepreciationRun, Long> {

    Optional<DepreciationRun> findByRequestId(String requestId);
}
