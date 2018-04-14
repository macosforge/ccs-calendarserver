#!/bin/sh

# This is because OpenShift runs with random UIDs,
# and ccs expects the UID to be in /etc/passwd
PASSWD_TEMP_FILE="/home/ccs/contrib/docker/passwd.template"
PASSWD_FILE="/tmp/passwd"

export USER_ID=$(id -u)
export GROUP_ID=$(id -g)
envsubst < $PASSWD_TEMP_FILE > $PASSWD_FILE

export LD_PRELOAD=/usr/lib/libnss_wrapper.so
export NSS_WRAPPER_PASSWD=$PASSWD_FILE
export NSS_WRAPPER_GROUP=/etc/group

# Just get our conf file
CCS_CONF_TEMP_FILE="/home/ccs/contrib/docker/caldavd.plist.template"

# It is important that this dir is world-writable,
# /tmp usually is
CCS_CONF_FILE="/tmp/caldavd.plist"

# Replace any env variable as they come from docker run
envsubst < $CCS_CONF_TEMP_FILE > $CCS_CONF_FILE

# Run caldavd, no daemonize, log to stdout
caldavd -X -L -f $CCS_CONF_FILE
