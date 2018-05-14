# CCS dockerised

This container of [Calendar and Contacts Server](https://github.com/apple/ccs-calendarserver/)
is thought to be run in production (in Kubernetes or OpenShift).

It uses `setup.py` and expects external __Postgres__ and __Memcached__.

__Postgres__ schema must be manually defined during the first run,
using [current.sql](https://github.com/apple/ccs-calendarserver/blob/master/txdav/common/datastore/sql_schema/current.sql).

What is being done in our `docker-compose.yml` is adding the SQL file in
`/docker-entrypoint-initdb.d` as suggested
[here](https://hub.docker.com/_/postgres/), at chapter:
*How to extend this image*.

## Running

### Docker Swarm
```bash
$ docker stack deploy -c docker-compose.yml ccs-stack
```

### K8s / OpenShift
__TODO__


## Configuration

Configuration of CCS is done in multiple layers:

1. `caldavd.envsubst.plist`, on which env variable are replaced by `docker_entrypoint.sh`. This takes care of loading:
2. `/etc/caldavd/caldavd.ext.plist`, which may be added via a *VOLUME*, envsubst is applied.
3. `/etc/caldavd/caldavd.writable.plist`, as a writable config file is required by CCS