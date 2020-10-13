#!/bin/sh

CREATEREPO=$(which createrepo_c)
if [ $? -ne 0 ]; then
    CREATEREPO="createrepo"
fi

# For backward compatibility.  Old repo location or new?
CENTOS_REPO=${MY_REPO}/centos-repo
if [ ! -d ${CENTOS_REPO} ]; then
    CENTOS_REPO=${MY_REPO}/cgcs-centos-repo
    if [ ! -d ${CENTOS_REPO} ]; then
        echo "ERROR: directory ${MY_REPO}/centos-repo not found."
        exit 1
    fi
fi

LOCAL_REPO=${MY_REPO}/local-repo
if [ ! -d ${LOCAL_REPO} ]; then
    LOCAL_REPO=${MY_REPO}/cgcs-tis-repo
    if [ ! -d ${LOCAL_REPO} ]; then
        # This one isn't fatal, LOCAL_REPO is not required
        LOCAL_REPO=${MY_REPO}/local-repo
    fi
fi

# If a file listed in list.txt is missing, this function attempts to find the
# RPM and copy it to the local directory.  This should not be required normally
# and is only used when collecting the source RPMs initially.
function findSrc {
    local lookingFor=$1
    find ${CENTOS_REPO}/Source -name $lookingFor | xargs -I '{}' cp '{}' .
    find ${LOCAL_REPO}/Source -name $lookingFor | xargs -I '{}' cp '{}' .
    find $MY_WORKSPACE/std/rpmbuild/SRPMS -name $lookingFor | xargs -I '{}' cp '{}' .
}

rm -f success.txt
rm -f fail.txt
rm -f missing.txt
mkdir -p results
infile=list.txt

while read p; do

    if [ ! -f "$p" ]; then
        findSrc $p
        if [ ! -f "$p" ]; then
            echo "couldn't find" >> missing.txt
            echo "couldn't find $p" >> missing.txt
            continue
        fi
        echo "found $p"
    fi

    mock -r build.cfg $p --resultdir=results --no-clean
    if [ $? -eq 0 ]; then
        echo "$p" >> success.txt
        cd results
        $CREATEREPO .
        cd ..
    else
        echo "$p" >> fail.txt
    fi
done < $infile
