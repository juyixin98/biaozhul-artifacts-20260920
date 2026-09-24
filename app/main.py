"""FastAPI 应用：六轴机械臂数值逆运动学服务。"""

from __future__ import annotations

import numpy as np
from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse

from . import __version__
from .config import load_crypto_config, load_solver_config
from .core.angles import wrap_to_pi
from .core.ik import IKStatus, solve_ik
from .core.robot_model import RobotModel
from .schemas import IKRequest, IKResponse
from .security import NonceCache, auth_dependency


def create_app() -> FastAPI:
    app = FastAPI(
        title="六轴串联机械臂数值逆运动学服务",
        version=__version__,
        description=(
            "阻尼最小二乘 + 多初值的 6R 机械臂 IK。"
            "成功结果均经过独立正运动学核验；不可达 / 奇异附近不收敛 / 限位冲突分开返回。"
        ),
    )
    robot = RobotModel.from_params()
    solver_cfg = load_solver_config()
    crypto_cfg = load_crypto_config()
    # 鉴权开启却没有密钥：启动即失败，杜绝空密钥静默放行
    if crypto_cfg.require_auth and not crypto_cfg.api_key:
        raise RuntimeError(
            "IK_REQUIRE_AUTH=true（默认）但未设置 IK_API_KEY 环境变量。"
            "请导出 IK_API_KEY 后再启动，或显式设置 IK_REQUIRE_AUTH=false（仅限本地测试）。"
        )
    app.state.robot = robot
    app.state.solver_config = solver_cfg
    app.state.crypto_config = crypto_cfg
    app.state.nonce_cache = NonceCache(crypto_cfg.timestamp_tolerance)

    @app.get("/healthz", tags=["ops"])
    def healthz() -> dict:
        return {"status": "ok", "service": "ik-service", "version": __version__}

    @app.get("/api/v1/robot/info", tags=["config"])
    def robot_info() -> dict:
        """返回 DH 参数、关节限位与求解器容差（与实际计算完全一致的同一份配置）。"""
        return {
            "name": robot.name,
            "dh_convention": "standard",
            "dh": [
                {
                    "joint": i + 1,
                    "a": float(robot.a[i]),
                    "d": float(robot.d[i]),
                    "alpha": float(robot.alpha[i]),
                    "theta_offset": float(robot.theta_offset[i]),
                }
                for i in range(robot.n_joints)
            ],
            "joint_limits_rad": {
                "lower": [float(x) for x in robot.joint_lower],
                "upper": [float(x) for x in robot.joint_upper],
            },
            "solver": solver_cfg.__dict__,
            "auth_required": crypto_cfg.require_auth,
        }

    @app.post(
        "/api/v1/ik",
        response_model=IKResponse,
        dependencies=[Depends(auth_dependency)],
        tags=["ik"],
    )
    def inverse_kinematics(req: IKRequest) -> IKResponse:
        T_des = np.eye(4)
        T_des[:3, 3] = np.asarray(req.position, dtype=float)
        T_des[:3, :3] = req.orientation.to_matrix()

        current = np.asarray(req.current_joints, dtype=float) if req.current_joints else None
        extra = (
            [np.asarray(s, dtype=float) for s in req.extra_seeds]
            if req.extra_seeds
            else None
        )

        result = solve_ik(
            app.state.robot, app.state.solver_config, T_des, current, extra
        )

        return IKResponse(
            status=result.status,
            success=result.status == IKStatus.SUCCESS,
            message=result.message,
            joints=[float(x) for x in result.q] if result.q is not None else None,
            joints_wrapped=[float(x) for x in wrap_to_pi(result.q)]
            if result.q is not None
            else None,
            position_error=result.position_error,
            orientation_error=result.orientation_error,
            candidates=[c.to_dict() for c in result.candidates],
            diagnostics=result.diagnostics,
        )

    return app


app = create_app()
