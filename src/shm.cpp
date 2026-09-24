// SPDX-License-Identifier: MIT
#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif
#include "shmrq/shm.h"

#include <cerrno>
#include <cstdio>
#include <cstring>
#include <fcntl.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <unistd.h>

namespace shmrq {

Err shm_validate_name(const char* name) noexcept {
  if (!name || name[0] != '/' || name[1] == '\0' || strlen(name) >= 100)
    return Err::InvalidName;
  for (const char* p = name + 1; *p; ++p)
    if (*p == '/')
      return Err::InvalidName;
  return Err::Ok;
}

static void close_desc(ShmDesc& d) {
  if (d.base && d.base != MAP_FAILED)
    munmap(d.base, d.bytes);
  if (d.fd >= 0)
    close(d.fd);
  d.base = nullptr;
  d.fd = -1;
  d.bytes = 0;
}

ShmDesc::~ShmDesc() { close_desc(*this); }

ShmDesc::ShmDesc(ShmDesc&& o) noexcept
    : fd(o.fd), base(o.base), bytes(o.bytes) {
  std::snprintf(name, sizeof(name), "%s", o.name);
  o.fd = -1;
  o.base = nullptr;
  o.bytes = 0;
}

ShmDesc& ShmDesc::operator=(ShmDesc&& o) noexcept {
  if (this != &o) {
    close_desc(*this);
    fd = o.fd;
    base = o.base;
    bytes = o.bytes;
    std::snprintf(name, sizeof(name), "%s", o.name);
    o.fd = -1;
    o.base = nullptr;
    o.bytes = 0;
  }
  return *this;
}

static Err map_segment(int fd, size_t bytes, ShmDesc& out) {
  void* p = mmap(nullptr, bytes, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
  if (p == MAP_FAILED) {
    close(fd);
    out.fd = -1;
    return Err::NoMem;
  }
  out.fd = fd;
  out.base = p;
  out.bytes = bytes;
  return Err::Ok;
}

Err shm_create(const char* name, size_t bytes, ShmDesc& out) noexcept {
  if (shm_validate_name(name) != Err::Ok)
    return Err::InvalidName;
  int fd = shm_open(name, O_RDWR | O_CREAT | O_EXCL, 0600);
  if (fd < 0)
    return (errno == EEXIST) ? Err::Incompatible : Err::NoMem;
  if (ftruncate(fd, static_cast<off_t>(bytes)) != 0) {
    close(fd);
    shm_unlink(name);
    return Err::NoMem;
  }
  Err e = map_segment(fd, bytes, out);
  if (e == Err::Ok)
    std::snprintf(out.name, sizeof(out.name), "%s", name);
  else
    shm_unlink(name);
  return e;
}

Err shm_open_existing(const char* name, ShmDesc& out) noexcept {
  if (shm_validate_name(name) != Err::Ok)
    return Err::InvalidName;
  int fd = shm_open(name, O_RDWR, 0600);
  if (fd < 0)
    return (errno == ENOENT) ? Err::Incompatible : Err::NoMem;
  struct stat st {};
  if (fstat(fd, &st) != 0 || static_cast<size_t>(st.st_size) < sizeof(Header)) {
    close(fd);
    return Err::Incompatible;
  }
  Err e = map_segment(fd, static_cast<size_t>(st.st_size), out);
  if (e == Err::Ok)
    std::snprintf(out.name, sizeof(out.name), "%s", name);
  return e;
}

Err shm_unlink_name(const char* name) noexcept {
  if (shm_validate_name(name) != Err::Ok)
    return Err::InvalidName;
  if (shm_unlink(name) != 0 && errno != ENOENT)
    return Err::NoMem;
  return Err::Ok;
}

// ----------------------------------------------------------------- OFD locks

static flock gate_flock(Gate g, short type) {
  flock lk{};
  lk.l_type = type;
  lk.l_whence = SEEK_SET;
  lk.l_start = (g == Gate::Producer) ? 0 : 1; // one byte each, in segment
  lk.l_len = 1;
  lk.l_pid = 0; // OFD locks do not use l_pid
  return lk;
}

#ifndef F_OFD_SETLK
#define F_OFD_SETLK 37
#define F_OFD_GETLK 36
#endif

Err shm_gate_lock(ShmDesc& d, Gate g) noexcept {
  flock lk = gate_flock(g, F_WRLCK);
  if (fcntl(d.fd, F_OFD_SETLK, &lk) != 0)
    return (errno == EACCES || errno == EAGAIN) ? Err::Locked : Err::NoMem;
  return Err::Ok;
}

Err shm_gate_unlock(ShmDesc& d, Gate g) noexcept {
  flock lk = gate_flock(g, F_UNLCK);
  if (fcntl(d.fd, F_OFD_SETLK, &lk) != 0)
    return Err::NoMem;
  return Err::Ok;
}

bool shm_gate_held(ShmDesc& d, Gate g) noexcept {
  flock lk = gate_flock(g, F_WRLCK);
  if (fcntl(d.fd, F_OFD_GETLK, &lk) != 0)
    return false;
  return lk.l_type != F_UNLCK;
}

uint64_t boot_id_now() noexcept {
  FILE* f = std::fopen("/proc/sys/kernel/random/boot_id", "r");
  uint64_t v = 0;
  if (f) {
    if (std::fscanf(f, "%16lx", &v) != 1)
      v = 0;
    std::fclose(f);
  }
  return v;
}

} // namespace shmrq
