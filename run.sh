#!/usr/bin/env bash
# Start the command center.  ./run.sh        = pretend robot (mock mode)
#                            ./run.sh robot  = real robot (needs ROS 2 and KIRO_OPERATOR_PIN)
set -eo pipefail
cd "$(dirname "$0")"
[ -x .venv/bin/python ] || { python3 -m venv --system-site-packages .venv && .venv/bin/pip install -q -r requirements.txt; }
if [ "${1:-}" = "robot" ]; then
  source /opt/ros/jazzy/setup.bash
  export KIRO_BACKEND=ros
else
  export KIRO_BACKEND="${KIRO_BACKEND:-mock}"
fi
exec .venv/bin/python -m robot_server
