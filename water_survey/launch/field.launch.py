"""Start the installed vessel bringup and one survey dashboard."""
import os
from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import IncludeLaunchDescription
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch_ros.actions import Node


def generate_launch_description():
    base = os.path.join(get_package_share_directory('mechaship_bringup'),
                        'launch', 'mechaship_bringup.launch.py')
    return LaunchDescription([
        IncludeLaunchDescription(PythonLaunchDescriptionSource(base),
                                 launch_arguments={'survey_dashboard': 'false'}.items()),
        Node(package='water_survey', executable='survey_dashboard',
             name='water_survey_dashboard', output='screen',
             parameters=[{'allow_hardware': True}]),
    ])
