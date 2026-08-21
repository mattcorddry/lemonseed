#!/bin/bash

set -e

cd "$(dirname -- "${BASH_SOURCE[0]}")" || exit 1

URI="https://lemonseed.ai/releases/stable/"
SCP_TGT="fubar-ssh.badbit.net:/websites/lemonseed.ai/releases/stable/"

echo "# $0 :: push release files to $URI"

if [ -f ../out/latest/manifest.json ] ; then
    MN_SUM="$( sha1sum ../out/latest/manifest.json | awk '{print $1}' )"
    echo "# rsync ../out/latest $SCP_TGT"
    rsync -rv -e ssh ../out/latest/ $SCP_TGT
else
    echo "!! ../out/latest/manifest.json is missing"
    exit 1
fi

REMOTE_MN_SUM="$( curl -fs ${URI}/manifest.json | sha1sum | awk '{print $1}' )"
if [ -z "$REMOTE_MN_SUM" ] ; then
    echo "!! unable to checksum ${URI}/manifest.json"
    exit 1
elif [ "$MN_SUM" != "$REMOTE_MN_SUM" ] ; then
    echo "!! manifest.json checksum mismatch after copy"
    echo "   local: $MN_SUM"
    echo "   remote: $REMOTE_MN_SUM"
    exit 1
else
    echo "# changes pushed and confirmed."
fi

exit 0
