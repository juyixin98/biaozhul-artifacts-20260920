#include "util.h"

#include <cerrno>
#include <atomic>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <ctime>
#include <fcntl.h>
#include <fstream>
#include <random>
#include <signal.h>
#include <sys/stat.h>
#include <thread>
#include <unistd.h>

namespace pgo {

Pose Compose(const Pose& a, const Pose& b) {
  Pose out;
  const double c = std::cos(a.theta), s = std::sin(a.theta);
  out.x = a.x + c * b.x - s * b.y;
  out.y = a.y + s * b.x + c * b.y;
  out.theta = NormalizeAngle(a.theta + b.theta);
  return out;
}

Pose Inverse(const Pose& a) {
  Pose inv;
  const double c = std::cos(a.theta), s = std::sin(a.theta);
  inv.x = -(c * a.x + s * a.y);
  inv.y = s * a.x - c * a.y;
  inv.theta = NormalizeAngle(-a.theta);
  return inv;
}

std::array<double, 3> EdgeResidual(const Pose& from, const Pose& to,
                                   const Pose& z) {
  // Ceres pose-graph 2D convention:
  //   e_xy = R(z)^T ( R(from)^T (t_to - t_from) - t_z )
  //   e_t  = wrap((to.t - from.t) - z.t)
  const double dx = to.x - from.x;
  const double dy = to.y - from.y;
  const double cf = std::cos(from.theta), sf = std::sin(from.theta);
  const double px = cf * dx + sf * dy;  // R(from)^T * d
  const double py = -sf * dx + cf * dy;
  const double rx = px - z.x;
  const double ry = py - z.y;
  const double cz = std::cos(z.theta), sz = std::sin(z.theta);
  std::array<double, 3> e;
  e[0] = cz * rx + sz * ry;  // R(z)^T * residual
  e[1] = -sz * rx + cz * ry;
  e[2] = NormalizeAngle((to.theta - from.theta) - z.theta);
  return e;
}

bool Approx(double a, double b, double tol) {
  return std::abs(a - b) <= tol * (1.0 + std::max(std::abs(a), std::abs(b)));
}

std::string UtcTimestamp() {
  std::time_t now = std::time(nullptr);
  std::tm tmv{};
  gmtime_r(&now, &tmv);
  char buf[32];
  std::strftime(buf, sizeof(buf), "%Y-%m-%dT%H:%M:%SZ", &tmv);
  return buf;
}

std::string RandomHex8() {
  static thread_local std::mt19937_64 rng(
      std::random_device{}() ^
      static_cast<uint64_t>(std::chrono::steady_clock::now()
                                .time_since_epoch().count()));
  static const char* hex = "0123456789abcdef";
  uint64_t v = rng();
  std::string out(16, '0');
  for (int i = 0; i < 16; ++i) out[i] = hex[(v >> (4 * i)) & 0xF];
  return out;
}

std::string NewRunId() {
  static std::atomic<unsigned long> seq{0};
  unsigned long n = seq.fetch_add(1);
  return UtcTimestamp() + "-" + std::to_string(getpid()) + "-" +
         std::to_string(n) + "-" + RandomHex8();
}

bool ReadFile(const std::string& path, std::string* out, std::string* err) {
  std::ifstream f(path, std::ios::binary);
  if (!f) {
    if (err) *err = "cannot open '" + path + "': " + std::strerror(errno);
    return false;
  }
  std::string data((std::istreambuf_iterator<char>(f)),
                   std::istreambuf_iterator<char>());
  if (f.bad()) {
    if (err) *err = "read error on '" + path + "'";
    return false;
  }
  *out = std::move(data);
  return true;
}

bool WriteFileAtomic(const std::string& path, const std::string& content,
                     std::string* err) {
  // tmp file in the same directory, fsync, rename — readers never see a
  // partial file, and cancellation can never publish a half result.
  std::string tmp = path + ".tmp-" + std::to_string(getpid()) + "-" + RandomHex8();
  int fd = ::open(tmp.c_str(), O_WRONLY | O_CREAT | O_EXCL, 0644);
  if (fd < 0) {
    if (err) *err = "create '" + tmp + "' failed: " + std::strerror(errno);
    return false;
  }
  size_t off = 0;
  while (off < content.size()) {
    ssize_t n = ::write(fd, content.data() + off, content.size() - off);
    if (n < 0) {
      if (errno == EINTR) continue;
      if (err) *err = "write '" + tmp + "' failed: " + std::strerror(errno);
      ::close(fd);
      ::unlink(tmp.c_str());
      return false;
    }
    off += static_cast<size_t>(n);
  }
  if (::fsync(fd) != 0) {
    if (err) *err = "fsync '" + tmp + "' failed: " + std::strerror(errno);
    ::close(fd);
    ::unlink(tmp.c_str());
    return false;
  }
  ::close(fd);
  if (::rename(tmp.c_str(), path.c_str()) != 0) {
    if (err) *err = "rename -> '" + path + "' failed: " + std::strerror(errno);
    ::unlink(tmp.c_str());
    return false;
  }
  return true;
}

namespace {
volatile sig_atomic_t g_cancel = 0;
void HandleSignal(int) { g_cancel = 1; }
}  // namespace

void InstallSignalHandlers() {
  struct sigaction sa {};
  sa.sa_handler = HandleSignal;
  sigemptyset(&sa.sa_mask);
  sigaction(SIGTERM, &sa, nullptr);
  sigaction(SIGINT, &sa, nullptr);
}

bool IsCancelRequested() { return g_cancel != 0; }

void RequestCancel() { g_cancel = 1; }

void SleepMs(int ms) {
  if (ms > 0) std::this_thread::sleep_for(std::chrono::milliseconds(ms));
}

}  // namespace pgo
