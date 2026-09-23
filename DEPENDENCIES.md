# 依赖锁定 / Dependency Lock

本项目**不依赖任何第三方库**（无 Maven/Gradle 坐标，无外部 jar），
HTTP 服务使用 JDK 自带的 `com.sun.net.httpserver`，JSON 为自实现的解析器。

## 工具链

| 工具 | 锁定版本 | 实测构建/运行版本 |
| --- | --- | --- |
| Java / javac | 17（语言级别 17，record/switch 表达式） | OpenJDK 17.0.20.1 (amd64, Ubuntu 24.04) |

## 第三方坐标

无。`src/main` 与 `src/test` 的全部 import 均来自 `java.*` / `javax.*` /
`com.sun.net.httpserver.*`（JDK 内置，非独立依赖）。

因此：

- 不存在需要下载的构件，离线环境可直接 `./scripts/build.sh`；
- 供应链面只有 JDK 本身；
- 复现基线 = JDK 17 + 本仓库源码（不使用任何会漂移的外部版本）。
