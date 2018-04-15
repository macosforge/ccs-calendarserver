#!/bin/sh

# This is because OpenShift runs with random UIDs,
# and ccs expects the UID to be in /etc/passwd
# Must be done at runtime because of the dynamic UID
echo "ccs:x:$(id -u):$(id -g):Calendar and Contacts Server:/home/ccs:/bin/bash" >> /etc/passwd

# Just get our conf file
CCS_CONF_TEMP_FILE="/home/ccs/contrib/docker/caldavd.plist.template"

# It is important that this dir is world-writable,
# /tmp usually is
CCS_CONF_FILE="/tmp/caldavd.plist"

# Replace any env variable as they come from docker run
envsubst < $CCS_CONF_TEMP_FILE > $CCS_CONF_FILE

# Run caldavd, no daemonize, log to stdout
caldavd -X -L -f $CCS_CONF_FILE
