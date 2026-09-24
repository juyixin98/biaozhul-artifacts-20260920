// SPDX-License-Identifier: MIT
// POSIX shared memory segment + open-file-description byte-range locks.
#pragma once

#include <cstddef>
#include <cstdint>

#include "shmrq/ring.h"

namespace shmrq {

enum class Gate : int {
  Producer = 0, // lock byte 0
  Consumer = 1, // lock byte 1
};

struct ShmDesc {
  int fd = -1;
  void* base = nullptr;
  size_t bytes = 0;
  char name[128] = {};

  ShmDesc() = default;
  ~ShmDesc();
  ShmDesc(const ShmDesc&) = delete;
  ShmDesc& operator=(const ShmDesc&) = delete;
  ShmDesc(ShmDesc&& o) noexcept;
  ShmDesc& operator=(ShmDesc&& o) noexcept;
};

// name must begin with '/' and contain no further slashes.
Err shm_validate_name(const char* name) noexcept;

// O_EXCL create at exactly `bytes`, then ftruncate + MAP_SHARED.
Err shm_create(const char* name, size_t bytes, ShmDesc& out) noexcept;
// Attach to an existing segment.
Err shm_open_existing(const char* name, ShmDesc& out) noexcept;
// Remove the name (mappings survive until closed; recovery note in README).
Err shm_unlink_name(const char* name) noexcept;

// OFD locks (F_OFD_SETLK) are owned by the open file description, not the
// thread/process, and are released automatically by the kernel on close —
// including when a process is SIGKILLed.  That is what lets a restarted
// producer tell "old producer dead" apart from "another live producer".
//
// Non-blocking: returns Err::Locked while a live owner holds the gate.
Err shm_gate_lock(ShmDesc& d, Gate g) noexcept;
Err shm_gate_unlock(ShmDesc& d, Gate g) noexcept;
// Test whether a live process currently holds the gate (F_OFD_GETLK).
bool shm_gate_held(ShmDesc& d, Gate g) noexcept;

uint64_t boot_id_now() noexcept;

} // namespace shmrq
