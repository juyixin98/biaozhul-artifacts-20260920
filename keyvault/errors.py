"""服务层异常类型。"""


class KeyVaultError(Exception):
    """所有密钥服务异常的基类。"""


class KeyNotFoundError(KeyVaultError):
    """引用了不存在的密钥版本。"""


class InvalidStateTransitionError(KeyVaultError):
    """请求的状态迁移不被状态机允许。"""


class KeyDestroyedError(KeyVaultError):
    """密钥版本已销毁，其密文明确不可恢复。"""


class NoActiveKeyError(KeyVaultError):
    """当前没有激活版本，无法加密。"""


class DecryptError(KeyVaultError):
    """解密失败（密文损坏、被篡改或标签不符）。"""


class WrongVersionError(DecryptError):
    """调用方指定的版本与密文实际加密版本不一致。"""
