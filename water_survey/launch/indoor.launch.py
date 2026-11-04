from launch import LaunchDescription
from launch_ros.actions import Node

def generate_launch_description():
    return LaunchDescription([Node(package='water_survey',executable='survey_dashboard',
        name='water_survey_dashboard',output='screen',parameters=[{'allow_hardware':False}])])
