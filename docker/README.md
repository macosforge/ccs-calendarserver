# CCS dockerised

This container of [Calendar and Contacts Server](https://github.com/apple/ccs-calendarserver/)
is thought to be run in production (in Kubernetes or OpenShift).

It uses `setup.py` and expects external __Postgres__ and __Memcached__.

__Postgres__ schema must be manually defined during the first run,
using [current.sql](https://github.com/apple/ccs-calendarserver/blob/master/txdav/common/datastore/sql_schema/current.sql).