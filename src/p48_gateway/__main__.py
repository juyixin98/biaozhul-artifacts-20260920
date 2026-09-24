"""Entry points: ``python -m p48_gateway.gateway`` / ``.robot`` / ``.client``."""

from __future__ import annotations

import argparse

import uvicorn

from .api import create_app
from .gateway_runtime import GatewayRuntime
from .registry import load_registry
from .robot_runtime import SyntheticRobot
from .topics import validate_robot_id


def run_gateway() -> None:
    parser = argparse.ArgumentParser(description="P048 command gateway")
    parser.add_argument("--registry", default="config/registry.example.json")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8088)
    args = parser.parse_args()

    runtime = GatewayRuntime(args.registry)
    app = create_app(runtime)
    try:
        uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    finally:
        runtime.shutdown()


def run_robot() -> None:
    parser = argparse.ArgumentParser(description="P048 synthetic robot")
    parser.add_argument("--registry", default="config/registry.example.json")
    parser.add_argument("--robot", required=True, type=validate_robot_id)
    args = parser.parse_args()

    registry = load_registry(args.registry)
    robot = registry.robots.get(args.robot)
    if robot is None:
        raise SystemExit(f"robot {args.robot!r} not in registry")
    node = SyntheticRobot(
        robot_id=robot.robot_id,
        namespace=robot.namespace,
        epoch_key=registry.epoch_key,
        testers=registry.testers,
    )
    print(f"robot {args.robot} up on namespace {robot.namespace}; Ctrl-C to stop")
    try:
        import time

        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        node.stop()
