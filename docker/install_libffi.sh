#!/bin/bash
set -xeo pipefail

LIBFFI_SHA256="cbb7f0b3b095dc506387ec1e35b891bfb4891d05b90a495bc69a10f2293f80ff"
LIBFFI_VERSION="3.4.8"

function check_sha256sum {
    local fname=$1
    local sha256=$2
    echo "${sha256}  ${fname}" > "${fname}.sha256"
    sha256sum -c "${fname}.sha256"
    rm "${fname}.sha256"
}

curl -sS -#O "https://mirrors.ocf.berkeley.edu/debian/pool/main/libf/libffi/libffi_${LIBFFI_VERSION}.orig.tar.gz"
check_sha256sum "libffi_${LIBFFI_VERSION}.orig.tar.gz" ${LIBFFI_SHA256}
tar zxf libffi*.orig.tar.gz
PATH=/opt/perl/bin:$PATH
pushd libffi*
STACK_PROTECTOR_FLAGS="-fstack-protector-strong"
if [ "$2" == "m32" ]; then
  setarch i686 ./autogen.sh
  setarch i386 ./configure --prefix=/usr/local CFLAGS="-m32 -g -O2 $STACK_PROTECTOR_FLAGS -Wformat -Werror=format-security"
else
  ./autogen.sh
  ./configure --prefix=/usr/local CFLAGS="-g -O2 $STACK_PROTECTOR_FLAGS -fPIC -Wformat -Werror=format-security"
fi
make install
popd
rm -rf libffi*
