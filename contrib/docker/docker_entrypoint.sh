#!/bin/bash
set -e

# This is because OpenShift runs with random UIDs,
# and ccs expects the UID to be in /etc/passwd
# Must be done at runtime because of the dynamic UID
echo "ccs:x:$(id -u):$(id -g):Calendar and Contacts Server:/home/ccs:/bin/bash" >> /etc/passwd

# Just get our conf file
CCS_CONF_TEMP_FILE="/home/ccs/contrib/docker/caldavd.envsubst.plist"

# It is important that this dir is world-writable,
# /tmp usually is
export CCS_CONF_FILE="/tmp/caldavd.plist"

# This file may be added by the user in a volume
CCS_USER_CONF_FILE="/etc/caldavd/caldavd.ext.plist"

# Replace any env variable as they come from docker run
envsubst < $CCS_CONF_TEMP_FILE > $CCS_CONF_FILE

# Doesn't work in-place,
# doesn't make much sense to have either:
# as the user is already defining their config...
#
# Replace env variables in user defined config if exists
# if [ -f $CCS_USER_CONF_FILE ]; then
#     envsubst < $CCS_USER_CONF_FILE > $CCS_USER_CONF_FILE
# fi

exec "$@"
