import pytest

from keyvault import KeyService


@pytest.fixture()
def data_dir(tmp_path):
    return tmp_path / "kvdata"


@pytest.fixture()
def svc(data_dir):
    return KeyService(data_dir)


@pytest.fixture()
def active_svc(svc):
    """已有一个激活版本 v1 的服务。"""
    svc.generate_key()
    svc.activate("v1")
    return svc
