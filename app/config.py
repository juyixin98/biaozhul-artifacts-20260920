"""Configuration for the fee-settlement HTTP service.

Everything is environment-driven with local-Anvil defaults. The default key is
Anvil's well-known first test account — never use it anywhere else.
"""
import os

# Anvil default account #0 (public test key, local chain only).
ANVIL_TEST_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"

RPC_URL = os.environ.get("RPC_URL", "http://127.0.0.1:8545")
PRIVATE_KEY = os.environ.get("PRIVATE_KEY", ANVIL_TEST_KEY)
CONTRACT_ADDRESS = os.environ.get("CONTRACT_ADDRESS", "")
DEPLOYMENT_FILE = os.environ.get("DEPLOYMENT_FILE", "deployment.json")
FORGE_OUT = os.environ.get("FORGE_OUT", "out")
