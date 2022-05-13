#!/bin/bash

PROGNAME="$(basename "$0")"

REQUIRED_PROGS="${PYTHON3:-python3}"

for prog in ${REQUIRED_PROGS} tar ar ; do
    if ! $prog --version >/dev/null 2>&1 ; then
        echo "$PROGNAME: WARNING: can't find \"$prog\", skipping tests" >&2
        exit 0
    fi
done

source "$(dirname "$0")"/../deb-utils.sh || exit 1
source "$(dirname "$0")"/../utils.sh || exit 1

declare -i FAIL_COUNT=0

# Usage: expect EXPECTED ACTUAL [DEPTH]
function expect {
    local expected="$1"
    local actual="$2"
    if [[ "${actual}" != "${expected}" ]] ; then
        let depth="${3:-0}"
        echo >&2
        echo "${BASH_SOURCE[0]}:${BASH_LINENO[${depth}]}: expectation failed:" >&2
        echo "    actual: [$actual]" >&2
        echo "  expected: [$expected]" >&2
        echo >&2
        return 1
    fi
    return 0
}

# Usage: echo ACTUAL | expect_stdin EXPECTED
function expect_stdin {
    expect "$1" "$(cat)" 1
}

#########################################################
# deb_get_field
#########################################################

#####################
echo "\
Dummy1: dummy1
Key1: value1
Dummy2: dummy2
"   | deb_get_field "Key1" \
    | expect_stdin "value1" \
|| let ++FAIL_COUNT

#####################
echo "

# 1st para
Dummy1: dummy1
Key1: value1
Dummy2: dummy2

# 2nd para
Dummy3: dummy3
Key1: value1
Dummy4: dummy4

"   | deb_get_field "Key1" \
    | expect_stdin "value1" \
|| let ++FAIL_COUNT

#####################
echo "

# 1st para
Dummy1: dummy1
Dummy2: dummy2

# 2nd para
Dummy3: dummy3
Key1: value1
Dummy4: dummy4

"   | deb_get_field "Key1" \
    | expect_stdin "" \
|| let ++FAIL_COUNT

#####################
echo "

Dummy1: dummy1
Key1: value1_line1
 value1_line2
 value1_line3
Dummy2: dummy2_line1
 dummy2_line2
 dummy2_line3

"   | deb_get_field "Key1" \
    | expect_stdin $'value1_line1\nvalue1_line2\nvalue1_line3' \
|| let ++FAIL_COUNT

#####################
echo "

Dummy1: dummy1
Key1: value1_line1
 value1_line2

"   | deb_get_field "Key1" \
    | expect_stdin $'value1_line1\nvalue1_line2' \
|| let ++FAIL_COUNT

#####################
echo "

Dummy1: dummy1
Key1: value1_line1
 value1_line2
Dummy2: dummy2

"   | deb_get_field "Key1" \
    | expect_stdin $'value1_line1\nvalue1_line2' \
|| let ++FAIL_COUNT


#####################
echo $'

Dummy1: dummy1
Key1: value1_line1
\tvalue1_line2
Dummy2: dummy2_line1
\tdummy2_line2

'   | deb_get_field "Key1" \
    | expect_stdin $'value1_line1\nvalue1_line2' \
|| let ++FAIL_COUNT


#########################################################
# deb_get_simple_depends
#########################################################

echo "
Depends: texinfo (>= 1.0), kernel-headers-2.2.10 [!hurd-i386],
 hurd-dev [hurd-i386], gnumach-dev [hurd-i386], yy-foo (>= 1.0) | zz-bar
"   | deb_get_simple_depends \
    | expect_stdin $'gnumach-dev\nhurd-dev\nkernel-headers-2.2.10\ntexinfo\nyy-foo\nzz-bar' \
|| let ++FAIL_COUNT


if [[ $FAIL_COUNT -gt 0 ]] ; then
    echo >&2
    echo "ERROR: ${FAIL_COUNT} test(s) failed" >&2
    echo >&2
    exit 1
fi
echo "$PROGNAME: all tests passed" >&2
exit 0

