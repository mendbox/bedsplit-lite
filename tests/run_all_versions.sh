#!/bin/zsh
# Usage: tests/run_all_versions.sh <out dir>   Runs tests/test_addon.py on Blender 4.2, 4.5 and 5.2, one after another.
# ONLY="5.2" limits it to one version.
O=${1:-/tmp/bedsplit-lite-test}; D=${0:A:h}
typeset -A BL
BL=(4.2 ~/Applications/blender-versions/Blender-4.2.23.app/Contents/MacOS/Blender
    4.5 ~/Applications/blender-versions/Blender-4.5.14.app/Contents/MacOS/Blender
    5.2 /Applications/Blender.app/Contents/MacOS/Blender)
mkdir -p $O
for v in ${=ONLY:-4.2 4.5 5.2}; do
  $BL[$v] -b --factory-startup --python $D/test_addon.py -- $O/$v > $O/$v.log 2>&1
  grep -E "^ADDON|Traceback" $O/$v.log || echo "ADDON $v CRASH (see $O/$v.log)"
  grep "^CHECK.* FAIL" $O/$v.log
done
