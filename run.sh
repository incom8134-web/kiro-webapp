#!/usr/bin/env bash
# Start the command center.  ./run.sh        = pretend robot (mock mode)
#                            ./run.sh robot  = real robot (needs ROS 2; asks for the PINs)
# The PINs are typed each time (hidden, never saved), unless KIRO_OPERATOR_PIN is already set.
set -eo pipefail
cd "$(dirname "$0")"
[ -x .venv/bin/python ] || { python3 -m venv --system-site-packages .venv && .venv/bin/pip install -q -r requirements.txt; }
if [ "${1:-}" = "robot" ]; then
  source /opt/ros/jazzy/setup.bash
  export KIRO_BACKEND=ros
  if [ -z "${KIRO_OPERATOR_PIN:-}" ] && [ -t 0 ]; then
    read -rsp "Operator PIN: " KIRO_OPERATOR_PIN; echo
    read -rsp "Engineer PIN (Enter = engineer tools off): " KIRO_ENGINEER_PIN; echo
    export KIRO_OPERATOR_PIN KIRO_ENGINEER_PIN
  fi
else
  export KIRO_BACKEND="${KIRO_BACKEND:-mock}"
fi
exec .venv/bin/python -m robot_server
