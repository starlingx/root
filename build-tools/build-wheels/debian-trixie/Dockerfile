# Start with an the old-ish trixie release (13.0), then upgrade --
# to make sure packages that come pre-installed in the debian:XXX image
# are older than anything in StarlingX.
ARG RELEASE=13.0
FROM debian:${RELEASE}

ENV DEBIAN_FRONTEND=noninteractive

ARG BUILD_STREAM=stable

ARG OS_CODENAME=trixie

# Disable upstream debian repos
# Trixie uses deb822 format: /etc/apt/sources.list.d/debian.sources
RUN mv /etc/apt/sources.list.d/debian.sources /etc/apt/sources.list.d/debian.sources.disabled

# Install apt repos
COPY debian-${OS_CODENAME}/apt/debian.sources.list /etc/apt/sources.list.d/debian.list
COPY debian-${OS_CODENAME}/apt/stx.sources.list /etc/apt/sources.list.d/stx.list
COPY debian-${OS_CODENAME}/apt/stx.preferences /etc/apt/preferences.d/stx

# Clean apt cache
RUN apt-get clean && rm -rf /var/lib/apt/lists/*

# Upgrade base packages to versions in managed repos and install ca-certificates
RUN mv /etc/apt/sources.list.d/debian.list /etc/apt/sources.list.d/debian.list.disabled && \
    apt-get -y update && \
    apt-get -y --no-install-recommends --no-install-suggests install ca-certificates && \
    apt-get -y upgrade && \
    mv /etc/apt/sources.list.d/debian.list.disabled /etc/apt/sources.list.d/debian.list && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# FIXME: disable vanilla trixie repo. Requires all dependent packages
# referenced by apt-get to be added to stx-tools .lst file(s). Otherwise
# we get a "random" set of packages, some from upstream, some from STX.
# We may also get package conflicts between vanilla debian & STX repos.

# Install the necessary packages for building the python modules.
# Some of these are dependencies of the specific modules, and could
# instead be added to the wheels.cfg file in the future.
RUN set -ex ; \
    apt-get -y update ; \
    apt-get -y --no-install-recommends --no-install-suggests install \
        bzip2 \
        cython3 \
        g++ \
        gcc \
        git \
        libev-dev \
        liberasurecode-dev \
        libffi-dev \
        libkrb5-dev \
        libldap2-dev \
        libmariadb-dev \
        libnss3-dev \
        libpcre2-dev \
        libpq-dev \
        libsasl2-dev \
        libsystemd-dev \
        libvirt-dev \
        libxml2-dev \
        libxslt1-dev \
        pkg-config \
        python3 \
        python3-dev \
        python3-nss \
        python3-pip \
        python3-setuptools \
        python3-setuptools-scm \
        python3-wheel \
        swig \
        unzip \
        wget \
        zip \
    ;

# These are required to build the python lxml module
ENV XML2_CONFIG=/usr/bin/xml2-config XSLT_CONFIG=/usr/bin/xslt-config


# APT clean up
RUN apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Copy local files
COPY docker-common/docker-build-wheel.sh /
COPY debian-${OS_CODENAME}/${BUILD_STREAM}-wheels.cfg /wheels.cfg
