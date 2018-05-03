FROM ubuntu:16.04

ENV VERSION "9.2"

RUN apt-get update \
    && apt-get -y install build-essential \
        python-setuptools python-pip python-dev \
        git curl gettext-base wget postgresql-client \
        libssl-dev libreadline6-dev libkrb5-dev libffi-dev  \
        libldap2-dev libsasl2-dev zlib1g-dev  \
    && apt-get purge -y --auto-remove \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt

RUN git clone https://github.com/apple/ccs-calendarserver.git ccs \
    && cd ccs \
    && git checkout CalendarServer-$VERSION

WORKDIR /opt/ccs

# Dependencies are retrieved and CCS installed in /usr/local
RUN pip install -r requirements-default.txt

ADD docker_cmd.sh bin
ADD caldavd.plist.template .

RUN useradd -ms /bin/bash ccs \
    && mkdir -p /var/db/caldavd /var/log/caldavd /var/run/caldavd /etc/caldavd /opt/ccs/src/twextpy/twext/python/__pycache__/ \
    && chown root:ccs /etc/caldavd /var/log/caldavd /var/run/caldavd /opt/ccs/src/twextpy/twext/python/__pycache__/  \
    && chown ccs:ccs /var/db/caldavd \
    && chmod -R g+rwX /opt/ccs /var/db/caldavd /var/log/caldavd /var/run/caldavd /etc/caldavd /opt/ccs/src/twextpy/twext/python/__pycache__/  \
    && chmod g=u /etc/passwd \
    && chmod +x /opt/ccs/bin/docker_cmd.sh

RUN  apt-get update && apt-get -y install emacs-nox iputils-ping telnet 

VOLUME [ "/var/db/caldavd", "/etc/caldavd" ]

EXPOSE 8080

# Some sensible defaults for config
ENV POSTGRES_HOST   tcp:postgres:5432
ENV POSTGRES_DB     postgres
ENV POSTGRES_USER   postgres
ENV POSTGRES_PASS   password
ENV MEMCACHED_HOST  memcached
ENV MEMCACHED_PORT  11211

USER ccs

# This entry point starts the server and creates the config files (in case they do not already exist)
CMD [ "/opt/ccs/bin/docker_cmd.sh" ]