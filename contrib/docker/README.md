# CCS dockerised

This container of [Calendar and Contacts Server](https://github.com/apple/ccs-calendarserver/)
is thought to be run in production (in Kubernetes or OpenShift).

It uses `setup.py` and expects external __Postgres__ and __Memcached__.

__Postgres__ schema must be manually defined during the first run,
using [current.sql](https://github.com/apple/ccs-calendarserver/blob/master/txdav/common/datastore/sql_schema/current.sql).


## Configuration

Configuration of CCS is done in multiple layers:

1. `caldavd.base.plist`, is added as is to the image. Is not to be used alone: it is imported by:
2. `caldavd.envsubst.plist`, on which env variable are replaced by `docker_entrypoint.sh`. This takes care of loading:
3. `/etc/caldavd/caldavd.ext.plist`, which may be added via a *VOLUME*.