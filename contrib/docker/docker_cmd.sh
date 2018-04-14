#!/bin/sh

# This is because OpenShift runs with random UIDs,
# and ccs expects the UID to be in /etc/passwd
export USER_ID=$(id -u)
export GROUP_ID=$(id -g)
envsubst < "/home/ccs/contrib/docker/passwd.template" > /tmp/passwd
export LD_PRELOAD=/usr/lib/libnss_wrapper.so
export NSS_WRAPPER_PASSWD=/tmp/passwd
export NSS_WRAPPER_GROUP=/etc/group

# Just get our conf file
TEMP_FILE="/home/ccs/contrib/docker/caldavd.plist.template"

# It is important that this dir is world-writable,
# /tmp usually is
CONF_FILE="/tmp/caldavd.plist"

# Replace any env variable as they come from docker run
envsubst < $TEMP_FILE > $CONF_FILE

# Run caldavd, no daemonize, log to stdout
caldavd -X -L -f $CONF_FILE
