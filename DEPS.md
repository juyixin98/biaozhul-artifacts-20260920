# 依赖与版本锁定

本项目仅依赖工具链与系统库，**无任何第三方 C/C++ 库**。所有同步原语自包含。

## 运行时（目标机必须）

| 依赖 | 最低版本 | 开发机实测版本 | 备注 |
|---|---|---|---|
| Linux kernel | 4.0 | 6.8.0-90-generic | OFD 锁（F_OFD_SETLK，内核 3.15+）、共享 futex |
| glibc | 2.27 起应可 | 2.39 (Ubuntu 24.04) | `shm_open`(librt)、`shm_unlink` |
| /dev/shm | tmpfs | 已挂载 | POSIX 共享内存由 tmpfs 支撑 |
| bash | 4.x | 5.2.21 | 集成夹具脚本 |
| python3 | 3.6 | 3.12 | 仅夹具里生成定长输入行，不进核心代码 |

## 构建时（目标机或 CI）

| 工具 | 最低版本 | 编译选项 |
|---|---|---|
| g++ | 10（C++17，`std::atomic<uint64_t>` 必须对所有目标 lock-free） | `-std=c++17 -O3`（Release 默认）或调试 `-O1` TSan |
| clang++ | 12 亦可 | 同左；已用 18 验证编译（见下） |
| cmake | 3.10 | 3.28.3 |
| GNU make | 4.x | 4.3 |

## 版本快照（开发机精确版本，供复现）

```
g++ 13.3.0 (Ubuntu 13.3.0-6ubuntu2~24.04.1)
cmake 3.28.3
make 4.3
bash 5.2.21
python 3.12.3
kernel 6.8.0-90-generic #91-Ubuntu SMP x86_64
glibc 2.39
```

## 为什么不需要锁文件

- C++ 系统头、Linux UAPI（`<linux/futex.h>`、`F_OFD_*`）随系统固定，
  无 fetch 渠道；这就是本项目的"锁定"——随内核/glibc 的稳定用户空间 ABI。
- CMake 不下载任何依赖；`check_library_exists(rt shm_open ...)` 仅在旧 glibc
  （2.33 以前 shm_open 在 librt）链 librt，在 2.39 上该检查自然跳过。
- 测试用的 strace 可选；如需观察真实 futex 调用：
  `strace -f -e futex ./build/shmrq_unit`。

## 安全说明

- shm 段以 mode 0600 创建，仅属主可访问；门控用 OFD 锁而非 PID 检测。
- CRC32 是完整性标签，非密码学 MAC；没有"密码操作"可锁定，README 已如实说明。
