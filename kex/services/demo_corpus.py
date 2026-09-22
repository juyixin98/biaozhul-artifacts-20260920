"""Runnable sample corpus — real multi-domain texts, not canned results."""
from __future__ import annotations

SAMPLE_DOCUMENTS: list[tuple[str, str]] = [
    (
        "kafka-rollout.txt",
        """Kafka 3.6 升级简报

2024-03-18，李明在清华大学组织了 Apache Kafka 3.6 的升级评审。
Alice Chen 与 Acme Corp 的工程师确认 PostgreSQL 与 Kafka 的连接器兼容，
并决定于 二〇二四年四月二日 在生产环境灰度发布。
张伟负责监控，王芳负责回滚预案。""",
    ),
    (
        "conference-notes.txt",
        """United Nations Data Forum Notes

On September 9, 2024, Bob Müller from Peking University presented work on
Python 3.12 and SQLite WAL. Acme Corporation co-hosted the panel; 小李
asked about Flask integration and Dr. Chen answered in English.
联合国的数据团队计划在 2025-06-30 之前发布白皮书。""",
    ),
    (
        "release-plan.txt",
        """发布计划

二〇二五年十二月九日发布 1.0。老张会在发布前一周（2025/12/02）冻结数据库变更，
postgres 集群由北京大学的团队巡检。芳芳负责文档，明哥负责值班排班。""",
    ),
]
