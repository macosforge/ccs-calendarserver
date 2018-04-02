#!/bin/sh

# Just get our conf file
TEMP_FILE="/home/ccs/contrib/docker/caldavd.plist.template"

# It is important that this dir is world-writable,
# /tmp usually is
CONF_FILE="/tmp/caldavd.plist"

# Replace any env variable as they come from docker run
envsubst < $TEMP_FILE > $CONF_FILE

# Run caldavd, no daemonize, log to stdout
caldavd -X -L -f $CONF_FILE
