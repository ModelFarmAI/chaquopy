#!/usr/bin/env bash
set -e

# shellcheck source=/dev/null
. environment.sh

# default values and settings

PYTHON_APPLE_SUPPORT="Python-Apple-support"
PYTHON_VERSION=$(python --version | awk '{ print $2 }' | awk -F '.' '{ print $1 "." $2 }')

# dependencies to build

DEPENDENCIES="
    chaquopy-freetype \
    chaquopy-libjpeg \
    chaquopy-libogg \
    chaquopy-libpng \
    chaquopy-libxml2 \
    chaquopy-libiconv \
    chaquopy-curl \
    chaquopy-ta-lib \
    chaquopy-zbar \
    "

# build dependencies

pushd "${TOOLCHAINS}"
#
#if ! [ -d "${PYTHON_APPLE_SUPPORT}" ]; then
#  git clone -b 3.10 https://github.com/ModelFarmAI/Python-Apple-support.git
#fi
#
pushd "${PYTHON_APPLE_SUPPORT}"
#make
#make libFFI-wheels
#make OpenSSL-wheels
#make BZip2-wheels
#make XZ-wheels
popd
if ! [ -d "${PYTHON_VERSION}" ]; then
    ln -s  "${PYTHON_APPLE_SUPPORT}/support/iOS" "${PYTHON_VERSION}"
fi
popd
#
#echo ${DIST_DIR}
#
#
#rm -rf "${DIST_DIR}/bzip2" "${DIST_DIR}/libffi" "${DIST_DIR}/openssl" "${DIST_DIR}/xz" "${LOGS}/deps"
#mkdir -p "${DIST_DIR}" "${LOGS}/deps"
#mv -f "${TOOLCHAINS}/${PYTHON_APPLE_SUPPORT}/wheels/dist"/* "${DIST_DIR}"
rm -f "${LOGS}/success.log" "${LOGS}/fail.log"
touch "${LOGS}/success.log" "${LOGS}/fail.log"

for DEPENDENCY in ${DEPENDENCIES}; do
  printf "\n\n*** Building dependency %s ***\n\n" "${DEPENDENCY}"
  python build-wheel.py --toolchain "${TOOLCHAINS}" --python "${PYTHON_VERSION}" --os iOS "${DEPENDENCY}" 2>&1 | tee "${LOGS}/deps/${DEPENDENCY}.log"

  # shellcheck disable=SC2010
  if [ "$(ls "dist/${DEPENDENCY}" | grep -c py3)" -ge "2" ]; then
    echo "${DEPENDENCY}" >> "${LOGS}/success.log"
  else
    echo "${DEPENDENCY}" >> "${LOGS}/fail.log"
  fi
done

echo ""
echo "Packages built successfully:"
cat "${LOGS}/success.log"
echo ""
echo "Packages with errors:"
cat "${LOGS}/fail.log"
echo ""
echo "Completed successfully."
