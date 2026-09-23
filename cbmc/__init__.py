"""合约有界模型检查器 (Contract Bounded Model Checker)。

纯 Python 后端：用 Z3 对显式 JSON 的有限状态合约模型做有界模型检查 (BMC)，
用 FastAPI 暴露 HTTP 接口。不执行用户提供的任何代码，只解释 JSON 数据结构。
"""
