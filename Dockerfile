FROM debian:jessie-slim

# Install prerequisites
RUN apt-get update \
 && apt-get upgrade -y \
 && apt-get install -y libsasl2-dev python-dev libldap2-dev libssl-dev curl \
                       make gcc bzip2 libreadline-dev zlib1g-dev libkrb5-dev \
                       python-pip libffi-dev libffi6 memcached git python-xattr

ADD . /opt/ccs
WORKDIR /opt/ccs

# Run configuration script
RUN ./bin/develop

# Create a new user. The UUID can be changed, but it is necessary to set one if
# you want to mount the /data folder as a Docker volume
RUN useradd ccs-user -d /opt/ccs -u 1001
RUN chown -R ccs-user:ccs-user /opt/ccs

USER ccs-user
CMD ["./bin/run", "-n"]
