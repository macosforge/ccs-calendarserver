FROM ubuntu:16.04

LABEL maintainer                    = "giorgio.azzinnaro@gmail.com"                               \
      io.openshift.tags             = caldavd,ccs                                                 \
      io.openshift.wants            = memcached,postgres                                          \
      io.k8s.description            = "Calendar and Contacts Server is a CalDAV implementation"   \
      io.openshift.expose-services  = 8080:http

# Straight from CCS GitHub install guide
# except for gettext-base, which we need for "envsubst"
RUN apt-get update &&                                       \
    apt-get -y install build-essential                      \
        python-setuptools python-pip python-dev             \
        git curl gettext-base                               \
        libssl-dev libreadline6-dev libkrb5-dev libffi-dev  \
        libldap2-dev libsasl2-dev zlib1g-dev

# All of the source code is in here
ADD . /home/ccs

WORKDIR /home/ccs

# Dependencies are retrieved and CCS installed in /usr/local
RUN pip install -r requirements-default.txt 

# Create all runtime directories and ensure right permissions for OC
RUN mkdir -p /var/db/caldavd /var/log/caldavd /var/run/caldavd &&                   \
    chmod -R g+rwX /home/ccs /var/db/caldavd /var/log/caldavd /var/run/caldavd &&   \
    chmod g=u /etc/passwd

# TODO Check if everything is in this dir
VOLUME [ "/var/db/caldavd" ]

# For user defined complex configuration (e.g. accounts.xml, resources.xml)
# A configuration file can be placed at /etc/caldavd/caldavd.ext.plist
VOLUME [ "/etc/caldavd" ]

# This can be edited in docker/caldavd.plist.template > HTTPPort
EXPOSE 8080

# Some sensible defaults for config
ENV POSTGRES_HOST   tcp:postgres:5432
ENV POSTGRES_DB     postgres
ENV POSTGRES_USER   postgres
ENV POSTGRES_PASS   password
ENV MEMCACHED_HOST  memcached
ENV MEMCACHED_PORT  11211

# To avoid errors with OpenShift, could be any
USER 1000

# This entry point simply creates /tmp/caldavd.plist,
# using the given ENV as placeholders
ENTRYPOINT [ "/home/ccs/contrib/docker/docker_entrypoint.sh" ]
CMD [ "caldavd", "-X", "-L", "-f", "$CCS_CONF_FILE" ]