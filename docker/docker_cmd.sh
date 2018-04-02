#!/bin/sh

# Just get our conf file
TEMP_FILE="/home/ccs/docker/caldavd.plist.template"

# It is important that this dir is world-writable,
# /tmp usually is
CONF_FILE="/tmp/caldavd.plist"

# Replace any env variable as they come from docker run
envsubst < $TEMP_FILE > $CONF_FILE

# TODO Evaluate performance issues
# This is because the random user picked by OpenShift
# is not allowed to write/mkdir __pycache__
export PYTHONDONTWRITEBYTECODE=x

# Run caldavd, no daemonize, log to stdout
caldavd -X -L -f $CONF_FILE
