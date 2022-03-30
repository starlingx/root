ARG RELEASE=7.5.1804
FROM centos:${RELEASE}

ARG BUILD_STREAM=stable

# Install the necessary packages for building the python modules.
# Some of these are dependencies of the specific modules, and could
# instead be added to the wheels.cfg file in the future.
RUN set -ex ;\
    sed -i '/\[main\]/ atimeout=120' /etc/yum.conf ;\
    yum makecache ;\
# nss>3.53.1 causes compile errors with some wheels
    nss_rpms=$(echo nss nss-util nss-tools nss-sysinit nss-softokn \
              nss-softokn-devel nss-softokn-freebl nss-devel \
              nss-util-devel nss-softokn-freebl-devel) ;\
    # install/upgrade all NSS packages @ v3.53.1
    yum install -y $(echo $nss_rpms | awk -v RS=' ' '{print $1 "-3.53.1"}') ;\
    # add "exclude=$nss_rpms" to the CentOS repo file
    sed -i -r -e "/^\\s*[[]updates[]]/a exclude=$nss_rpms" /etc/yum.repos.d/CentOS-Base.repo ;\
# install required packages
    yum install -y epel-release centos-release-openstack-queens ;\
    yum install -y git gcc zip bzip2 unzip \
                   python3 python3-pip python3-wheel python3-devel \
                   wget openldap-devel mariadb mariadb-devel \
                   libvirt libvirt-devel liberasurecode-devel nss-devel \
                   systemd-devel postgresql-devel ;\
# pip<19.2.3 doesn't ignore yanked packages from pypi.org
    python3 -m pip install pip==19.2.3 ;\
# setuptools-scm's maintainers keep publishing and yanking new versions.
# Pin it to latest version known to work
    python3 -m pip install setuptools-scm==6.0.1 ;\
# while setuptools is larger than 45.3, it no longer support "Features" in setup.py
    python3 -m pip install --user setuptools==45.3 ;\
    python3 -m pip install --user --upgrade wheel
COPY docker-common/docker-build-wheel.sh /
COPY centos/${BUILD_STREAM}-wheels.cfg /wheels.cfg

# Python2 packages
RUN set -ex; \
    yum -y install python python-devel ;\
    wget https://bootstrap.pypa.io/pip/2.7/get-pip.py ;\
    python get-pip.py
COPY centos/${BUILD_STREAM}-wheels-py2.cfg /wheels-py2.cfg

# root CA cert expired on October 1st, 2021
RUN yum update -y ca-certificates
