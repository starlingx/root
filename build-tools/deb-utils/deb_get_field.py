#!/usr/bin/env python3

import sys, re

def usage():
    print ("""\
Usage: %s KEY...
Read a debian control file from STDIN, print KEY values to STDOUT
""" % sys.argv[0])

if len (sys.argv) > 0 and sys.argv[1] == "--help":
    usage()
    sys.exit(0)

# regex: "^(?:KEY1|KEY2|...)\s*:\s*(.*?)\s*$"
re_field = re.compile (
    "^(?:" +
    "|".join (
        [ re.escape (key) for key in sys.argv[1:] ]
    ) +
    "):\s*(.*?)\s*$"
)

re_ws = re.compile ("^\s*$")

in_header = True
past_1st_paragraph = False
in_multiline_field = False

for line in sys.stdin:

    # skip initial empty lines
    if in_header and re_ws.fullmatch (line):
        continue
    in_header = False

    # skip everything past the 1st block
    if past_1st_paragraph:
        continue
    if re_ws.fullmatch (line):
        past_1st_paragraph = True
        continue

    # Key: value
    match = re_field.fullmatch (line)
    if match:
        print (match.group(1))
        in_multiline_field = True
        continue

    # line starts with a space or tab: belongs to the previous field
    if in_multiline_field and (line.startswith (" ") or line.startswith ("\t")):
        print (line[1:].rstrip())
        continue

    in_multiline_field = False
