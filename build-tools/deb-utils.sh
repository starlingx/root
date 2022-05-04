# bash
# vim: set syn=sh:

__DEB_UTILS_DIR=$(readlink -f "$(dirname "${BASH_SOURCE[0]}")")/deb-utils

#
# Usage: __deb_get_section DEB_FILE {control|data}
#
# Uncompress and print the specified section to STDOUT in tar format.
# You should pipe it to "tar" to be useful.
#
function __deb_get_section {
    local deb_file="$1"
    local section="$2"

    # find $section.tar.{gz,bz2,xz}
    local section_entry
    section_entry="$(
        ar t "$deb_file" | \grep "^$section[.]" || true
    )" || return 1
    if [[ -z "$section_entry" ]] ; then
        echo "$deb_file: couldn't find ${section}.*" >&2
        return 1
    fi

    # untar it to stdout
    local uncompress
    case "${section_entry#${section}.}" in
        tar.gz | tgz)  uncompress="gunzip" ;;
        tar.bz2)       uncompress="bunzip2" ;;
        tar.xz)        uncompress="unxz" ;;
        *)
            echo "$deb_file: unsupported archive format $section_entry" >&2
            return 1
    esac
    ar p "$1" "$section_entry" | $uncompress
    check_pipe_status
}

#
# Usage: deb_get_control DEB_FILE
#
# Print the control file from the specified DEB package
#
function deb_get_control {
    __deb_get_section "$1" control | tar -O -x ./control
    check_pipe_status
}

#
# Usage: deb_extract_content DEB_FILE [--verbose] [PATHS_IN_ARCHIVE...]
#
# Extract deb package content to current directory
#
function deb_extract_content {
    __deb_get_section "$1" data | tar -x
    check_pipe_status
}

#
# Usage: deb_get_field KEY...
#
# Read a debian control file from STDIN, find the specified fields
# and print their values on STDOUT. With multiple fields, their values
# will be merged in the output w/no separators.
#
# See: https://www.debian.org/doc/debian-policy/ch-controlfields.html
#
function deb_get_field {
    ${PYTHON3:-python3} "${__DEB_UTILS_DIR}/deb_get_field.py" "$@"
}

#
# Usage: deb_get_simple_depends
#
# Read debian control file from STDIN, then print its immediate runtime
# dependencies to STDOUT, one per line, stripping any conditions and
# operators, e.g.:
#
#   ...
#   Depends: aaa, bbb [!amd64], ccc | ddd (>= 1.0)
#   ...
#
# will be converted to
#
#   aaa
#   bbb
#   ccc
#   ddd
#
function deb_get_simple_depends {
    local raw_depends
    raw_depends=$(deb_get_field 'Pre-Depends' 'Depends') || return 1
    echo $raw_depends \
        | tr ',|' '\n' \
        | sed -r 's/^\s*([^[:space:](><=[]+).*$/\1/' \
        | grep -v -E '^\s*$' \
        | sort -u
}

