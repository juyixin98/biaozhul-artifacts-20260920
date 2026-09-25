"""训练检查点恢复：纯 NumPy 的本地机器学习基础设施服务。

核心能力：
- 合成数据上的小型线性模型训练（SGD + momentum）
- 检查点保存：模型参数、优化器状态、随机数状态、数据游标
- 断点恢复：与不中断训练在明确容差内一致（本实现为逐位一致）
- 损坏检查点检测与回退
"""

from .config import TrainConfig
from .trainer import Trainer
from .checkpoint import CorruptCheckpointError

__all__ = ["TrainConfig", "Trainer", "CorruptCheckpointError"]

__version__ = "0.1.0"
