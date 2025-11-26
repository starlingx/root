# Start with an the old-ish trixie release (13.0), then upgrade --
# to make sure packages that come pre-installed in the debian:XXX image
# are older than anything in StarlingX.
ARG RELEASE=13.0
FROM debian:${RELEASE}

ENV DEBIAN_FRONTEND=noninteractive

ARG BUILD_STREAM=stable

# Install latest ca-certificates
RUN apt-get -y update && \
    apt-get -y --no-install-recommends --no-install-suggests install ca-certificates

# Disable upstream debian repos
RUN mv /etc/apt/sources.list /etc/apt/sources.list.disabled

# Install apt repos
COPY debian/apt/debian.sources.list /etc/apt/sources.list.d/debian.list
COPY debian/apt/stx.sources.list /etc/apt/sources.list.d/stx.list
COPY debian/apt/stx.preferences /etc/apt/preferences.d/stx

# Clean apt cache
RUN apt-get clean && rm -rf /var/lib/apt/lists/*

# Upgrade base packages to versions in the managed repos
RUN mv /etc/apt/sources.list.d/debian.list /etc/apt/sources.list.d/debian.list.disabled && \
    apt-get -y update && \
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
        libpcre3-dev \
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
        python3-thriftpy \
        python3-wheel \
        swig \
        unzip \
        wget \
        zip \
    ; \
    # make sure python3-nss is sane \
    # the upstream version of it doesn't install pip metadata, but our patched version does
    if [ `find /usr/lib/python3/dist-packages -maxdepth 1 -type f -name 'python_nss*.egg-info' -print -quit | wc -l` -eq 0 ] ; then \
        echo "python-nss didn't install pip metadata!" >&2 ; \
        echo "Did you compile python-nss locally prior to building this docker image?" >&2 ; \
        exit 1 ; \
    fi

# These are required to build the python lxml module
ENV XML2_CONFIG=/usr/bin/xml2-config XSLT_CONFIG=/usr/bin/xslt-config

# Python2 environment
RUN set -ex ; \
    apt-get -y install \
        python \
        python-dev \
        python-setuptools \
    ; \
    wget https://bootstrap.pypa.io/pip/2.7/get-pip.py ; \
    python get-pip.py ; \
    rm -f get-pip.py


# APT clean up
RUN apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Copy local files
COPY docker-common/docker-build-wheel.sh /
COPY debian/${BUILD_STREAM}-wheels.cfg /wheels.cfg
COPY debian/${BUILD_STREAM}-wheels-py2.cfg /wheels-py2.cfg

