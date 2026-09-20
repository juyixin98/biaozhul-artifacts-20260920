from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException

from app import config


@dataclass
class Identity:
    name: str
    role: str  # manager | accountant | auditor


def get_identity(authorization: str = Header(default="")) -> Identity:
    """Bearer Token 认证。演示实现：令牌即角色，生产环境应替换为网关/OIDC。"""
    prefix = "Bearer "
    if not authorization.startswith(prefix):
        raise HTTPException(status_code=401, detail="missing bearer token")
    token = authorization[len(prefix):].strip()
    if token == config.MANAGER_TOKEN:
        return Identity(name="manager", role="manager")
    if token == config.ACCOUNTANT_TOKEN:
        return Identity(name="accountant", role="accountant")
    if token == config.AUDITOR_TOKEN:
        return Identity(name="auditor", role="auditor")
    raise HTTPException(status_code=401, detail="invalid token")


def require_write(identity: Identity = Depends(get_identity)) -> Identity:
    """审计员只读，其余角色可写。"""
    if identity.role == "auditor":
        raise HTTPException(status_code=403, detail="auditor is read-only")
    return identity


def require_manager(identity: Identity = Depends(get_identity)) -> Identity:
    """仅财务负责人（如重开期间）。"""
    if identity.role != "manager":
        raise HTTPException(status_code=403, detail="manager role required")
    return identity
