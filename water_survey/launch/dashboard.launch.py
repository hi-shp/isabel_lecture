from launch import LaunchDescription
from launch_ros.actions import Node
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration
from launch_ros.parameter_descriptions import ParameterValue


def generate_launch_description():
    return LaunchDescription([
        DeclareLaunchArgument('scan_topic',default_value='/scan'),
        DeclareLaunchArgument('scan_yaw_offset_deg',default_value='0.0'),
        Node(package='water_survey', executable='survey_dashboard',
        name='water_survey_dashboard', output='screen', parameters=[
            {'allow_hardware': True, 'manage_bringup': True, 'default_mode': 'real',
             'scan_topic':LaunchConfiguration('scan_topic'),
             'scan_yaw_offset_deg':ParameterValue(LaunchConfiguration('scan_yaw_offset_deg'),value_type=float)}])])
