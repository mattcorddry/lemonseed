#!/bin/bash

set -e

cd "$(dirname -- "${BASH_SOURCE[0]}")" || exit 1

URI="https://lemonseed.ai/releases/stable/"
SCP_HOST="fubar-ssh.badbit.net"
SCP_PATH="/websites/lemonseed.ai/releases/stable/"
SCP_TGT="${SCP_HOST}:${SCP_PATH}"
INSTALLER="../out/installer/lemonseed-installer.iso"

echo "# $0 :: push release files to $URI"

if [ -f ../out/latest/manifest.json ] ; then
    MN_SUM="$( sha1sum ../out/latest/manifest.json | awk '{print $1}' )"
    echo "# rsync ../out/latest $SCP_TGT"
    rsync -rvt --progress -e ssh ../out/latest/ $SCP_TGT
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
fi

if [ -e "$INSTALLER" ] ; then
    INST_SUM="$( sha1sum "$INSTALLER" | awk '{print $1}' )"
    echo "# rsync $INSTALLER $SCP_TGT"
    rsync -vt --progress -e ssh "$INSTALLER" "$SCP_TGT"
    INST_REMOTE_SUM="$( ssh $SCP_HOST sha1sum ${SCP_PATH}/lemonseed-installer.iso | awk '{print $1}' )"
    if [[ "$INST_SUM" == "$INST_REMOTE_SUM" ]] ; then
        echo "# installer checksum OK"
    else
        echo "!! installer checksum mismatch after copy"
        echo "   local file: $INSTALLER"
        echo "   local sha1: $INST_SUM"
        echo "   remote file: $SCP_TGT/lemonseed-installer.iso"
        echo "   remote sha1: $INST_REMOTE_SUM"
        exit 1
    fi
else
    echo "# skipping installer copy: not found"
    echo "# $INSTALLER"
fi

echo "# changes pushed and confirmed."

exit 0
