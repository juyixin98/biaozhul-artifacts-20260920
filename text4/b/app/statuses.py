"""Status constants shared by models and services."""
from __future__ import annotations

# ProgramVersion lifecycle
VERSION_DRAFT = "draft"
VERSION_PUBLISHED = "published"

# Enrollment lifecycle
ENR_PENDING = "pending"        # holds a seat, confirmation not yet received
ENR_CONFIRMED = "confirmed"    # seat confirmed, learner may progress
ENR_WAITLISTED = "waitlisted"  # no seat yet, ordered by waitlist_position
ENR_CANCELLED = "cancelled"    # learner gave the seat back
ENR_EXPIRED = "expired"        # 48h hold elapsed without confirmation

ACTIVE_ENROLLMENT_STATES = (ENR_PENDING, ENR_CONFIRMED, ENR_WAITLISTED)
SEAT_HOLDING_STATES = (ENR_PENDING, ENR_CONFIRMED)

# StepResult lifecycle
RESULT_PASSED = "passed"
RESULT_FAILED = "failed"
RESULT_SUBMITTED = "submitted"  # awaiting supervisor evaluation
