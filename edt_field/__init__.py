"""最近障碍距离场（Euclidean Distance Transform）离线计算库。

对二维占据栅格计算精确欧氏距离变换：每个栅格输出到最近障碍栅格的
欧氏距离以及该最近障碍的来源坐标。纯离线、纯合成数据，不依赖 ROS、
不连接硬件、不做可视化。
"""

from edt_field.core import DistanceField, euclidean_distance_transform

__all__ = ["DistanceField", "euclidean_distance_transform"]
__version__ = "0.1.0"
