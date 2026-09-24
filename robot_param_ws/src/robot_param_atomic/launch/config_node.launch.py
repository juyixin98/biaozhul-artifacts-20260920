from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def generate_launch_description():
    db_path = LaunchConfiguration('db_path')

    return LaunchDescription([
        DeclareLaunchArgument(
            'db_path',
            default_value='./robot_config.db',
            description='SQLite file backing the atomic configuration store'),
        Node(
            package='robot_param_atomic',
            executable='robot_config_node',
            name='robot_config',
            output='screen',
            parameters=[{'db_path': db_path}],
        ),
    ])
