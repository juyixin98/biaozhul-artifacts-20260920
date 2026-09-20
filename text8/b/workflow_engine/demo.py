"""演示流程与种子数据。"""

import json
from pathlib import Path

from sqlalchemy.orm import Session

from workflow_engine.schemas import TemplateDefinition
from workflow_engine.services import templates as tpl_service

DEMO_FILE = Path(__file__).resolve().parent.parent / "demo" / "expense_demo.json"


def load_demo_definition() -> dict:
    raw = json.loads(DEMO_FILE.read_text(encoding="utf-8"))
    # 走一遍 Pydantic 校验，保证演示文件本身合法
    return TemplateDefinition.model_validate(raw).model_dump()


def seed_demo(session: Session) -> dict:
    """幂等创建演示模板：不存在则创建并发布 v1，已存在则跳过。"""
    definition = load_demo_definition()
    key = definition["key"]
    from sqlalchemy import select

    from workflow_engine.models import Template

    template = session.scalar(select(Template).where(Template.key == key))
    if template is not None:
        return {"key": key, "version": template.current_version_number, "created": False}

    template, tv = tpl_service.create_template(session, definition, publish=True)
    return {"key": key, "version": tv.version, "created": True}
