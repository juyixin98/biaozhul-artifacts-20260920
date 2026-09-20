"""Idempotent demo data: the 'expense' approval flow.

::

    [开始] -> [经理审批 会签] -> <条件 amount >= 10000?>
                                 是 -> [总监审批 任签, 超时升级CFO] -> [通过]
                                 否(默认) ----------------------------> [通过]
    任一审批人明确拒绝 -> [驳回]
"""
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import catalog
from .models import Template
from .schemas import Definition, TemplateCreate

logger = logging.getLogger("wf.seed")

DEMO_DEFINITION = {
    "nodes": [
        {"id": "n_start", "type": "start", "name": "提交报销"},
        {
            "id": "n_manager",
            "type": "approval",
            "name": "经理会签",
            "mode": "all",
            "assignees": ["manager1", "manager2"],
        },
        {"id": "n_amount_check", "type": "condition", "name": "金额是否≥10000"},
        {
            "id": "n_director",
            "type": "approval",
            "name": "总监任签(超时升级CFO)",
            "mode": "any",
            "assignees": ["director1", "director2"],
            "timeout_seconds": 60,
            "escalate_to": ["cfo"],
        },
        {"id": "n_budget_check", "type": "condition", "name": "预算中心判断"},
        {"id": "n_end_ok", "type": "end", "name": "审批通过", "terminal": "approved"},
        {"id": "n_end_reject", "type": "end", "name": "预算驳回", "terminal": "rejected"},
    ],
    "edges": [
        {"source": "n_start", "target": "n_manager"},
        {"source": "n_manager", "target": "n_amount_check"},
        {"source": "n_amount_check", "target": "n_director", "expression": "amount >= 10000"},
        {"source": "n_amount_check", "target": "n_budget_check"},  # default edge
        {
            "source": "n_budget_check",
            "target": "n_end_reject",
            "expression": "budget_status == 'frozen'",
        },
        {"source": "n_budget_check", "target": "n_end_ok"},  # default edge
        {"source": "n_director", "target": "n_end_ok"},
    ],
}


def seed_demo(db: Session) -> None:
    existing = db.scalar(select(Template).where(Template.key == "expense"))
    if existing is not None:
        logger.info("demo template already exists; skipping seed")
        return
    body = TemplateCreate(
        key="expense",
        name="费用报销演示流程",
        definition=Definition.model_validate(DEMO_DEFINITION),
    )
    template = catalog.create_template(db, body)
    catalog.publish_version(db, template.key, 1)
    logger.info("demo template 'expense' v1 seeded and published")
