#!/bin/bash

# echo "ccs:x:$(id -u):$(id -g):Calendar and Contacts Server:/opt/ccs:/bin/bash" >> /etc/passwd

# Just get our conf file
CCS_CONF_TEMP_FILE="/opt/ccs/caldavd.plist.template"

# It is important that this dir is world-writable,
# /tmp usually is
CCS_CONF_FILE="/etc/caldavd/caldavd.plist"
CCS_AUTH_FILE="/etc/caldavd/accounts.xml"

# create SQL tables on first run
echo $(awk -F: '{print $2":"$3}' <<< $POSTGRES_HOST):$POSTGRES_DB:$POSTGRES_USER:$POSTGRES_PASS > ~/.pgpass
chmod 600 ~/.pgpass

# wait for postgres to be available
export PGHOST=$(awk -F: '{print $2}' <<< $POSTGRES_HOST)
until psql -h $PGHOST -U $POSTGRES_USER $POSTGRES_DB -c '\q'; do
  >&2 echo "Postgres is unavailable - sleeping"
  sleep 1
done

TD=$(psql -h $PGHOST -U $POSTGRES_USER $POSTGRES_DB <<< '\dt')

if [ "$TD" = "No relations found." ]; then
    psql -h $PGHOST -U $POSTGRES_USER $POSTGRES_DB < /opt/ccs/txdav/common/datastore/sql_schema/current.sql
fi

# create config file on first run
if [ ! -f $CCS_CONF_FILE ]; then
    envsubst < $CCS_CONF_TEMP_FILE > $CCS_CONF_FILE
fi

# create auth file if not available
if [ ! -f $CCS_AUTH_FILE ]; then
    cp /opt/ccs/conf/auth/accounts.xml $CCS_AUTH_FILE
    cp /opt/ccs/conf/auth/*.dtd /etc/caldavd
fi

# Run caldavd, no daemonize, log to stdout
caldavd -X -L -f $CCS_CONF_FILE

