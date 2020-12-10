# List of packages to be included/installed in ISO
# If these have dependencies, they will be pulled in automatically
#
acpid
bc
gdb
influxdb
python3-influxdb
python3-gunicorn
iperf3
isomd5sum
python3-aodhclient
python3-oslo-log
python3-six
python3-d2to1
hiera
python3-pecan
python3-configobj
python3-pycodestyle
python3-rsa
rubygem-ruby-shadow
swig
syslinux
iotop
linuxptp
procps-ng
python3-daemon
python3-pyudev
curl
lvm2
time
postgresql
postgresql-server
postgresql-contrib
targetcli
strace
wget
bind-utils
selinux-policy
#pm-utils
tcpdump
sysstat
smartmontools
collectd
puppet-collectd
socat
attr

# for realtime kernel
#rtctl
rt-setup

# For low-latency compute
#OVMF
edk2-ovmf

# neutron bgp
python3-pankoclient

# ima plugin for RPM
ntfs-3g
ntfsprogs
python3-memcached
python3-coverage

# kubernetes packages
docker-ce
etcd
#docker-forward-journald

# Add debugging tools
zip
unzip
traceroute

# support for persistent sessions
screen

# For kata container
kata-runtime

# For nvme disk firmware update
nvme-cli
