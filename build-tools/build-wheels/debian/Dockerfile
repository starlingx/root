ARG RELEASE=11.3
FROM debian:${RELEASE}

ARG BUILD_STREAM=stable

# Install apt repos
RUN mv /etc/apt/sources.list /etc/apt/sources.list.d/debian.list
COPY debian/apt/sources.list /etc/apt/sources.list.d/stx.list
COPY debian/apt/preferences /etc/apt/preferences.d/stx

# FIXME: disable upstream bullseye repo. Requires all dependent packages
# referenced by apt-get to be added to stx-tools .lst file(s). Otherwise
# we get a "random" set of packages, some from upstream, some from STX.

# FIXME: there's no ussuri/bullseye port, best we can do is "victoria" or more recent
#RUN set -ex ; \
#    echo "deb [trusted=yes] http://osbpo.debian.net/osbpo bullseye-victoria-backports-nochange main" \
#        /etc/apt/sources.list.d/openstack-victoria.list.disabled ; \

# Install the necessary packages for building the python modules.
# Some of these are dependencies of the specific modules, and could
# instead be added to the wheels.cfg file in the future.
RUN set -ex ; \
    apt-get -y update ; \
    apt-get -y --no-install-recommends --no-install-suggests install \
        bzip2 \
        ca-certificates \
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

