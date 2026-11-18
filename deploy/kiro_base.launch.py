"""Wheel base for the command center: mecanum_base_node + twist_mux.

  ros2 launch ~/kiro-webapp/deploy/kiro_base.launch.py            # web control only
  ros2 launch ~/kiro-webapp/deploy/kiro_base.launch.py joy:=true  # plus a gamepad plugged into the robot

Needs the existing so101_base package (source ~/ros2_ws/install/setup.bash first)
and twist_mux (sudo apt install ros-jazzy-twist-mux).
NOT yet tested on the robot.
"""
import os

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, GroupAction
from launch.conditions import IfCondition
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node

HERE = os.path.dirname(os.path.abspath(__file__))


def generate_launch_description():
    from ament_index_python.packages import get_package_share_directory

    joy_cfg = os.path.join(get_package_share_directory("so101_base"), "config", "teleop_xbox.yaml")
    return LaunchDescription([
        DeclareLaunchArgument("port", default_value="/dev/so101_base"),
        DeclareLaunchArgument("joy", default_value="false", description="also use a gamepad plugged into the robot"),
        Node(package="so101_base", executable="mecanum_base_node", name="mecanum_base", output="screen",
             parameters=[{"port": LaunchConfiguration("port")}]),
        Node(package="twist_mux", executable="twist_mux", name="twist_mux", output="screen",
             parameters=[os.path.join(HERE, "twist_mux.yaml")],
             remappings=[("cmd_vel_out", "cmd_vel")]),
        GroupAction(condition=IfCondition(LaunchConfiguration("joy")), actions=[
            Node(package="joy", executable="joy_node", name="joy_node",
                 parameters=[{"device_id": 0, "deadzone": 0.1, "autorepeat_rate": 20.0}]),
            Node(package="teleop_twist_joy", executable="teleop_node", name="teleop_twist_joy_node",
                 parameters=[joy_cfg], remappings=[("cmd_vel", "cmd_vel_joy")]),
        ]),
    ])
