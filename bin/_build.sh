# -*- sh-basic-offset: 2 -*-
##
# Copyright (c) 2005-2016 Apple Inc. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
##

. "${wd}/bin/_py.sh";


# Provide a default value: if the variable named by the first argument is
# empty, set it to the default in the second argument.
conditional_set () {
  local var="$1"; shift;
  local default="$1"; shift;
  if [ -z "$(eval echo "\${${var}:-}")" ]; then
    eval "${var}=\${default:-}";
  fi;
}


c_macro () {
  local sys_header="$1"; shift;
  local version_macro="$1"; shift;

  local value="$(printf "#include <${sys_header}>\n${version_macro}\n" | cc -x c -E - 2>/dev/null | tail -1)";

  if [ "${value}" = "${version_macro}" ]; then
    # Macro was not replaced
    return 1;
  fi;

  echo "${value}";
}


# Checks for presence of a C header, optionally with a version comparison.
# With only a header file name, try to include it, returning nonzero if absent.
# With 3 params, also attempt a version check, returning nonzero if too old.
# Param 2 is a minimum acceptable version number
# Param 3 is a #define from the source that holds the installed version number
# Examples:
#   Assert that ldap.h is present
#     find_header "ldap.h"
#   Assert that ldap.h is present with a version >= 20344
#     find_header "ldap.h" 20344 "LDAP_VENDOR_VERSION"
find_header () {
  local sys_header="$1"; shift;
  if [ $# -ge 1 ]; then
      local   min_version="$1"; shift;
      local version_macro="$1"; shift;
  fi;

  # No min_version given:
  # Check for presence of a header. We use the "-c" cc option because we don't
  # need to emit a file; cc exits nonzero if it can't find the header
  if [ -z "${min_version:-}" ]; then
    echo "#include <${sys_header}>" | cc -x c -c - -o /dev/null 2> /dev/null;
    return "$?";
  fi;

  # Check for presence of a header of specified version
  local found_version="$(c_macro "${sys_header}" "${version_macro}")";

  if [ -n "${found_version}" ] && cmp_version "${min_version}" "${found_version}"; then
    return 0;
  else
    return 1;
  fi;
};


has_valid_code_signature () {
  local p="$1"; shift;
  if [ -f ${p} ] && [ -x ${p} ]; then
    if /usr/bin/codesign -v ${p} > /dev/null 2>&1; then
      return 0;
    else
      return 1;
    fi;
  fi;
};


ad_hoc_sign_if_code_signature_is_invalid () {
  local p="$1"; shift;
  if [ -f ${p} ] && [ -x ${p} ]; then
    if has_valid_code_signature ${p}; then
      # If there is a valid code signature, we're done.
      echo "${p} has a valid code signature.";
      return 0;
    else
      # If codesign exits non-zero, that could mean either 'no signature' or
      # 'invalid signature'. We need to determine which one.
      # An unsigned binary has no LC_CODE_SIGNATURE load command.
      if /usr/bin/otool -l ${p} | /usr/bin/grep LC_CODE_SIGNATURE > /dev/null 2>&1; then
        # Has LC_CODE_SIGNATURE, but signature isn't valid. Ad-hoc sign it.
        echo "${p} has an invalid code signature, attempting to repair..."
        /usr/bin/codesign -f -s - ${p};
        # Validate the ad-hoc signature
        if has_valid_code_signature ${p}; then
          echo "Invalid code signature replaced via ad-hoc signing.";
          return 0;
        fi;
      else
        # No code signature.
        echo "${p} is not code signed. This is OK.";
        return 0;
      fi;
    fi;
  fi;
  return 1;
};


# Initialize all the global state required to use this library.
init_build () {
  cd "${wd}";

  init_py;

  # These variables are defaults for things which might be configured by
  # environment; only set them if they're un-set.
  conditional_set wd "$(pwd)";
  conditional_set do_get "true";
  conditional_set do_setup "true";
  conditional_set force_setup "false";
  conditional_set virtualenv_opts "";

      dev_home="${wd}/.develop";
     dev_roots="${dev_home}/roots";
  dep_packages="${dev_home}/pkg";
   dep_sources="${dev_home}/src";

  py_virtualenv="${dev_home}/virtualenv";
      py_bindir="${py_virtualenv}/bin";

  python="${bootstrap_python}";
  export PYTHON="${python}";

  if [ -z "${TWEXT_PKG_CACHE-}" ]; then
    dep_packages="${dev_home}/pkg";
  else
    dep_packages="${TWEXT_PKG_CACHE}";
  fi;

  project="$(setup_print name)" || project="<unknown>";

  dev_patches="${dev_home}/patches";
  patches="${wd}/lib-patches";

  # Find some hashing commands
  # sha1() = sha1 hash, if available
  # md5()  = md5 hash, if available
  # hash() = default hash function
  # $hash  = name of the type of hash used by hash()

  hash="";

  if find_cmd openssl > /dev/null; then
    if [ -z "${hash}" ]; then hash="md5"; fi;
    # remove "(stdin)= " from the front which openssl emits on some platforms
    md5 () { "$(find_cmd openssl)" dgst -md5 "$@" | sed 's/^.* //'; }
  elif find_cmd md5 > /dev/null; then
    if [ -z "${hash}" ]; then hash="md5"; fi;
    md5 () { "$(find_cmd md5)" "$@"; }
  elif find_cmd md5sum > /dev/null; then
    if [ -z "${hash}" ]; then hash="md5"; fi;
    md5 () { "$(find_cmd md5sum)" "$@"; }
  fi;

  if find_cmd sha1sum > /dev/null; then
    if [ -z "${hash}" ]; then hash="sha1sum"; fi;
    sha1 () { "$(find_cmd sha1sum)" "$@"; }
  fi;
  if find_cmd shasum > /dev/null; then
    if [ -z "${hash}" ]; then hash="sha1"; fi;
    sha1 () { "$(find_cmd shasum)" "$@"; }
  fi;

  if [ "${hash}" = "sha1" ]; then
    hash () { sha1 "$@"; }
  elif [ "${hash}" = "md5" ]; then
    hash () { md5 "$@"; }
  elif find_cmd cksum > /dev/null; then
    hash="hash";
    hash () { cksum "$@" | cut -f 1 -d " "; }
  elif find_cmd sum > /dev/null; then
    hash="hash";
    hash () { sum "$@" | cut -f 1 -d " "; }
  else
    hash () { echo "INTERNAL ERROR: No hash function."; exit 1; }
  fi;

  default_requirements="${wd}/requirements-default.txt";
  use_openssl="true";

  # Use SecureTransport instead of OpenSSL if possible, unless told otherwise
  if [ -z "${USE_OPENSSL-}" ]; then 
    if [ $(uname -s) = "Darwin" ]; then
      # SecureTransport requires >= 10.11 (Darwin >= 15).
      REV=$(uname -r)
      if (( ${REV%%.*} >= 15 )); then
        default_requirements="${wd}/requirements-osx.txt";
        use_openssl="false";
      else
        # Needed to build OpenSSL 64-bit on OS X
        export KERNEL_BITS=64;
      fi;
    fi;
  fi;  
  conditional_set requirements "${default_requirements}"
}


setup_print () {
  local what="$1"; shift;

  PYTHONPATH="${wd}:${PYTHONPATH:-}" "${bootstrap_python}" - 2>/dev/null << EOF
from __future__ import print_function
import setup
print(setup.${what})
EOF
}


# Apply patches from lib-patches to the given dependency codebase.
apply_patches () {
  local name="$1"; shift;
  local path="$1"; shift;

  if [ -d "${patches}/${name}" ]; then
    echo "";
    echo "Applying patches to ${name} in ${path}...";

    cd "${path}";
    find "${patches}/${name}"                  \
        -type f                                \
        -name '*.patch'                        \
        -print                                 \
        -exec patch -p0 --forward -i '{}' ';';
    cd /;

  fi;

  echo "";
  if [ -e "${path}/setup.py" ]; then
    echo "Removing build directory ${path}/build...";
    rm -rf "${path}/build";
    echo "Removing pyc files from ${path}...";
    find "${path}" -type f -name '*.pyc' -print0 | xargs -0 rm -f;
  fi;
}

# If do_get is turned on, get an archive file containing a dependency via HTTP.
www_get () {
  if ! "${do_get}"; then return 0; fi;

  local  md5="";
  local sha1="";

  local OPTIND=1;
  while getopts "m:s:" option; do
    case "${option}" in
      'm')  md5="${OPTARG}"; ;;
      's') sha1="${OPTARG}"; ;;
    esac;
  done;
  shift $((${OPTIND} - 1));

  local name="$1"; shift;
  local path="$1"; shift;
  local  url="$1"; shift;

  if "${force_setup}"; then
    rm -rf "${path}";
  fi;
  if [ ! -d "${path}" ]; then
    local ext="$(echo "${url}" | sed 's|^.*\.\([^.]*\)$|\1|')";
    local decompress="";
    local unpack="";

    untar () { tar -xvf -; }
    unzipstream () { local tmp="$(mktemp -t ccsXXXXX)"; cat > "${tmp}"; unzip "${tmp}"; rm "${tmp}"; }
    case "${ext}" in
      gz|tgz) decompress="gzip -d -c"; unpack="untar"; ;;
      bz2)    decompress="bzip2 -d -c"; unpack="untar"; ;;
      tar)    decompress="untar"; unpack="untar"; ;;
      zip)    decompress="cat"; unpack="unzipstream"; ;;
      *)
        echo "Error in www_get of URL ${url}: Unknown extension ${ext}";
        exit 1;
        ;;
    esac;

    echo "";

    if [ -n "${dep_packages}" ] && [ -n "${hash}" ]; then
      mkdir -p "${dep_packages}";

      local cache_basename="$(echo ${name} | tr '[ ]' '_')-$(echo "${url}" | hash)-$(basename "${url}")";
      local cache_file="${dep_packages}/${cache_basename}";

      check_hash () {
        local file="$1"; shift;

        local sum="$(md5 "${file}" | perl -pe 's|^.*([0-9a-f]{32}).*$|\1|')";
        if [ -n "${md5}" ]; then
          echo "Checking MD5 sum for ${name}...";
          if [ "${md5}" != "${sum}" ]; then
            echo "ERROR: MD5 sum for downloaded file is wrong: ${sum} != ${md5}";
            return 1;
          fi;
        else
          echo "MD5 sum for ${name} is ${sum}";
        fi;

        local sum="$(sha1 "${file}" | perl -pe 's|^.*([0-9a-f]{40}).*$|\1|')";
        if [ -n "${sha1}" ]; then
          echo "Checking SHA1 sum for ${name}...";
          if [ "${sha1}" != "${sum}" ]; then
            echo "ERROR: SHA1 sum for downloaded file is wrong: ${sum} != ${sha1}";
            return 1;
          fi;
        else
          echo "SHA1 sum for ${name} is ${sum}";
        fi;
      }

      if [ ! -f "${cache_file}" ]; then
        echo "No cache file: ${cache_file}";

        echo "Downloading ${name}...";

        #
        # Try getting a copy from the upstream source.
        #
        local tmp="$(mktemp "/tmp/${cache_basename}.XXXXXX")";
        curl -L "${url}" -o "${tmp}";
        echo "";

        if [ ! -s "${tmp}" ] || grep '<title>404 Not Found</title>' "${tmp}" > /dev/null; then
          rm -f "${tmp}";
          echo "${name} is not available from upstream source: ${url}";
          exit 1;
        elif ! check_hash "${tmp}"; then
          rm -f "${tmp}";
          echo "${name} from upstream source is invalid: ${url}";
          exit 1;
        fi;

        #
        # OK, we should be good
        #
        mv "${tmp}" "${cache_file}";
      else
        #
        # We have the file cached, just verify hash
        #
        if ! check_hash "${cache_file}"; then
          exit 1;
        fi;
      fi;

      echo "Unpacking ${name} from cache...";
      get () { cat "${cache_file}"; }
    else
      echo "Downloading ${name}...";
      get () { curl -L "${url}"; }
    fi;

    rm -rf "${path}";
    cd "$(dirname "${path}")";
    get | ${decompress} | ${unpack};
    apply_patches "${name}" "${path}";
    cd "${wd}";
  fi;
}


# Run 'make' with the given command line, prepending a -j option appropriate to
# the number of CPUs on the current machine, if that can be determined.
jmake () {
  local ncpu="";

  case "$(uname -s)" in
    Darwin|Linux)
      ncpu="$(getconf _NPROCESSORS_ONLN)";
      ;;
    FreeBSD)
      ncpu="$(sysctl hw.ncpu)";
      ncpu="${ncpu##hw.ncpu: }";
      ;;
  esac;

  case "${ncpu}" in
    ''|*[!0-9]*)
      make "$@" ;;
    *)
      make -j "${ncpu}" "$@" ;;
  esac;
}

# Declare a dependency on a C project built with autotools.
# Support for custom configure, prebuild, build, and install commands
# prebuild_cmd, build_cmd, and install_cmd phases may be skipped by
# passing the corresponding option with the empty string as the value.
# By default, do: ./configure --prefix ... ; jmake ; make install
c_dependency () {
  local f_hash="";
  local configure="configure";
  local prebuild_cmd=""; 
  local build_cmd="jmake"; 
  local install_cmd="make install";

  local OPTIND=1;
  while getopts "m:s:c:p:b:" option; do
    case "${option}" in
      'm') f_hash="-m ${OPTARG}"; ;;
      's') f_hash="-s ${OPTARG}"; ;;
      'c') configure="${OPTARG}"; ;;
      'p') prebuild_cmd="${OPTARG}"; ;;
      'b') build_cmd="${OPTARG}"; ;;
    esac;
  done;
  shift $((${OPTIND} - 1));

  local name="$1"; shift;
  local path="$1"; shift;
  local  uri="$1"; shift;

  # Extra arguments are processed below, as arguments to configure.

  mkdir -p "${dep_sources}";

  local srcdir="${dep_sources}/${path}";
  local dstroot="${dev_roots}/${name}";

  www_get ${f_hash} "${name}" "${srcdir}" "${uri}";

  export              PATH="${dstroot}/bin:${PATH}";
  export    C_INCLUDE_PATH="${dstroot}/include:${C_INCLUDE_PATH:-}";
  export   LD_LIBRARY_PATH="${dstroot}/lib:${dstroot}/lib64:${LD_LIBRARY_PATH:-}";
  export          CPPFLAGS="-I${dstroot}/include ${CPPFLAGS:-} ";
  export           LDFLAGS="-L${dstroot}/lib -L${dstroot}/lib64 ${LDFLAGS:-} ";
  export DYLD_LIBRARY_PATH="${dstroot}/lib:${dstroot}/lib64:${DYLD_LIBRARY_PATH:-}";
  export   PKG_CONFIG_PATH="${dstroot}/lib/pkgconfig:${PKG_CONFIG_PATH:-}";

  if "${do_setup}"; then
    if "${force_setup}"; then
        rm -rf "${dstroot}";
    fi;
    if [ ! -d "${dstroot}" ]; then
      echo "Building ${name}...";
      cd "${srcdir}";
      "./${configure}" --prefix="${dstroot}" "$@";
      if [ ! -z "${prebuild_cmd}" ]; then
        eval ${prebuild_cmd};
      fi;
      eval ${build_cmd};
      eval ${install_cmd};
      cd "${wd}";
    else
      echo "Using built ${name}.";
      echo "";
    fi;
  fi;
}


ruler () {
  if "${do_setup}"; then
    echo "____________________________________________________________";
    echo "";

    if [ $# -gt 0 ]; then
      echo "$@";
    fi;
  fi;
}


using_system () {
  if "${do_setup}"; then
    local name="$1"; shift;
    echo "Using system version of ${name}.";
    echo "";
  fi;
}


#
# Build C dependencies
#
c_dependencies () {
  local    c_glue_root="${dev_roots}/c_glue";
  local c_glue_include="${c_glue_root}/include";

  export C_INCLUDE_PATH="${c_glue_include}:${C_INCLUDE_PATH:-}";


  # The OpenSSL version number is special. Our strategy is to get the integer
  # value of OPENSSL_VERSION_NUBMER for use in inequality comparison.
  if [ "${use_openssl}" = "true" ]; then
    ruler;

    local min_ssl_version="268443791";  # OpenSSL 1.0.2h

    local ssl_version="$(c_macro openssl/ssl.h OPENSSL_VERSION_NUMBER)";
    if [ -z "${ssl_version}" ]; then ssl_version="0x0"; fi;
    ssl_version="$("${bootstrap_python}" -c "print ${ssl_version}")";

    if [ "${ssl_version}" -ge "${min_ssl_version}" ]; then
      using_system "OpenSSL";
    else
      local v="1.0.2h";
      local n="openssl";
      local p="${n}-${v}";

      # use 'config' instead of 'configure'; 'make' instead of 'jmake'.
      # also pass 'shared' to config to build shared libs.
      c_dependency -c "config" -s "577585f5f5d299c44dd3c993d3c0ac7a219e4949" \
        -p "make depend" -b "make" \
        "openssl" "${p}" \
        "http://www.openssl.org/source/${p}.tar.gz" "shared";
    fi;
  fi;


  ruler;
  if find_header ffi.h; then
    using_system "libffi";
  elif find_header ffi/ffi.h; then
    if "${do_setup}"; then
      mkdir -p "${c_glue_include}";
      echo "#include <ffi/ffi.h>" > "${c_glue_include}/ffi.h"
      using_system "libffi";
    fi;
  else
    local v="3.2.1";
    local n="libffi";
    local p="${n}-${v}";

    c_dependency -m "83b89587607e3eb65c70d361f13bab43" \
      "libffi" "${p}" \
      "ftp://sourceware.org/pub/libffi/${p}.tar.gz"
  fi;


  ruler;
  if find_header ldap.h 20428 LDAP_VENDOR_VERSION; then
    using_system "OpenLDAP";
  else
    local v="2.4.38";
    local n="openldap";
    local p="${n}-${v}";

    c_dependency -m "39831848c731bcaef235a04e0d14412f" \
      "OpenLDAP" "${p}" \
      "http://www.openldap.org/software/download/OpenLDAP/${n}-release/${p}.tgz" \
      --disable-bdb --disable-hdb --with-tls=openssl;
  fi;


  ruler;
  if find_header sasl.h; then
    using_system "SASL";
  elif find_header sasl/sasl.h; then
    if "${do_setup}"; then
      mkdir -p "${c_glue_include}";
      echo "#include <sasl/sasl.h>" > "${c_glue_include}/sasl.h"
      using_system "SASL";
    fi;
  else
    local v="2.1.26";
    local n="cyrus-sasl";
    local p="${n}-${v}";

    c_dependency -m "a7f4e5e559a0e37b3ffc438c9456e425" \
      "CyrusSASL" "${p}" \
      "ftp://ftp.cyrusimap.org/cyrus-sasl/${p}.tar.gz" \
      --disable-macos-framework;
  fi;


  ruler;
  if command -v memcached > /dev/null; then
    using_system "memcached";
  else
    local v="2.0.21-stable";
    local n="libevent";
    local p="${n}-${v}";

	local configure_openssl="--enable-openssl=yes";
    if [ "${use_openssl}" = "false" ]; then
	  local configure_openssl="--enable-openssl=no";
	fi;

    c_dependency -m "b2405cc9ebf264aa47ff615d9de527a2" \
      "libevent" "${p}" \
      "https://github.com/downloads/libevent/libevent/${p}.tar.gz" \
      ${configure_openssl};

    local v="1.4.24";
    local n="memcached";
    local p="${n}-${v}";

    c_dependency -s "32a798a37ef782da10a09d74aa1e5be91f2861db" \
      "memcached" "${p}" \
      "http://www.memcached.org/files/${p}.tar.gz" \
      "--disable-docs";
  fi;


  ruler;
  if command -v postgres > /dev/null; then
    using_system "Postgres";
  else
    local v="9.5.3";
    local n="postgresql";
    local p="${n}-${v}";

    if command -v dtrace > /dev/null; then
      local enable_dtrace="--enable-dtrace";
    else
      local enable_dtrace="";
    fi;

    c_dependency -m "3f0c388566c688c82b01a0edf1e6b7a0" \
      "PostgreSQL" "${p}" \
      "http://ftp.postgresql.org/pub/source/v${v}/${p}.tar.bz2" \
      ${enable_dtrace};
  fi;

}

#
# Special cx_Oracle patch handling
#
cx_Oracle_patch() {

  local f_hash="-m 6a49e1aa0e5b48589f8edfe5884ff5a5";
  local v="5.2";
  local n="cx_Oracle";
  local p="${n}-${v}";

  local srcdir="${dev_patches}/${p}";

  www_get ${f_hash} "${n}" "${srcdir}" "https://pypi.python.org/packages/source/c/${n}/${p}.tar.gz";
  cd "${dev_patches}";
  tar zcf "${p}.tar.gz" "${p}";
  cd "${wd}";
  rm -rf "${srcdir}";
}

#
# Build Python dependencies
#
py_dependencies () {
  python="${py_bindir}/python";
  py_ve_tools="${dev_home}/ve_tools";

  export PATH="${py_virtualenv}/bin:${PATH}";
  export PYTHON="${python}";
  export PYTHONPATH="${py_ve_tools}/lib:${wd}:${PYTHONPATH:-}";

  mkdir -p "${dev_patches}";

  # Work around a change in Xcode tools that breaks Python modules in OS X
  # 10.9.2 and prior due to a hard error if the -mno-fused-madd is used, as
  # it was in the system Python, and is therefore passed along by disutils.
  if [ "$(uname -s)" = "Darwin" ]; then
    if "${bootstrap_python}" -c 'import distutils.sysconfig; print distutils.sysconfig.get_config_var("CFLAGS")' \
       | grep -e -mno-fused-madd > /dev/null; then
      export ARCHFLAGS="-Wno-error=unused-command-line-argument-hard-error-in-future";
    fi;
  fi;

  if ! "${do_setup}"; then return 0; fi;

  # Set up virtual environment

  if "${force_setup}"; then
    # Nuke the virtual environment first
    rm -rf "${py_virtualenv}";
  fi;

  if [ ! -d "${py_virtualenv}" ]; then
    bootstrap_virtualenv;
    "${bootstrap_python}" -m virtualenv  \
      --system-site-packages             \
      --no-setuptools                    \
      ${virtualenv_opts}                 \
      "${py_virtualenv}";
    if [ "${use_openssl}" = "false" ]; then
      # Interacting with keychain requires either a valid code signature, or no
      # code signature. An *invalid* code signature won't work.
      if ! ad_hoc_sign_if_code_signature_is_invalid ${python}; then
        cat << EOF
SecureTransport support is enabled, but we are unable to validate or fix the
code signature for ${python}. Keychain interactions used with SecureTransport
require either no code signature, or a valid code signature. To switch to
OpenSSL, delete the .develop directory, export USE_OPENSSL=1, then run
./bin/develop again.
EOF
      fi;
    fi;
  fi;

  cd "${wd}";

  # Make sure setup got called enough to write the version file.

  PYTHONPATH="${PYTHONPATH}" "${python}" "${wd}/setup.py" check > /dev/null;

  if [ -d "${dev_home}/pip_downloads" ]; then
    pip_install="pip_install_from_cache";
  else
    pip_install="pip_download_and_install";
  fi;

  ruler "Preparing Python requirements";
  echo "";
  "${pip_install}" --requirement="${requirements}";

  for extra in $("${bootstrap_python}" -c 'import setup; print "\n".join(setup.extras_requirements.keys())'); do
    ruler "Preparing Python requirements for optional feature: ${extra}";
    echo "";

    if [ "${extra}" = "oracle" ]; then
      cx_Oracle_patch;
    fi;

    if ! "${pip_install}" --editable="${wd}[${extra}]"; then
      echo "Feature ${extra} is optional; continuing.";
    fi;
  done;

  ruler "Preparing Python requirements for patching";
  echo "";
  "${pip_install}" --ignore-installed --no-deps --requirement="${wd}/requirements-ignore-installed.txt";

  ruler "Patching Python requirements";
  echo "";
  twisted_version=$("${python}" -c 'from twisted import version; print version.base()');
  if [ ! -e "${py_virtualenv}/lib/python2.7/site-packages/twisted/.patch_applied.${twisted_version}" ]; then
    apply_patches "Twisted" "${py_virtualenv}/lib/python2.7/site-packages"
    find "${py_virtualenv}/lib/python2.7/site-packages/twisted" -type f -name '.patch_applied*' -print0 | xargs -0 rm -f;
    touch "${py_virtualenv}/lib/python2.7/site-packages/twisted/.patch_applied.${twisted_version}";
  fi;

  echo "";
}


macos_oracle () {
  if [ "${ORACLE_HOME-}" ]; then
    case "$(uname -s)" in
      Darwin)
        echo "macOS Oracle init."
        export   LD_LIBRARY_PATH="${LD_LIBRARY_PATH:-}:${ORACLE_HOME}";
        export DYLD_LIBRARY_PATH="${DYLD_LIBRARY_PATH:-}:${ORACLE_HOME}";
        ;;
    esac;
  fi;
}


bootstrap_virtualenv () {
  mkdir -p "${py_ve_tools}";
  export PYTHONUSERBASE="${py_ve_tools}";
  # If we're already in a venv, don't use --user flag for pip install
  if [ -z ${VIRTUAL_ENV:-} ]; then NESTED="--user" ; else NESTED=""; fi

  for pkg in             \
      setuptools==18.5    \
      pip==8.1.2          \
      virtualenv==15.0.2  \
  ; do
      ruler "Installing ${pkg}";
      "${bootstrap_python}" -m pip install -I ${NESTED} "${pkg}";
  done;
}


pip_download () {
  mkdir -p "${dev_home}/pip_downloads";

  "${python}" -m pip download               \
    --disable-pip-version-check            \
    -d "${dev_home}/pip_downloads" \
    --pre                                  \
    --no-cache-dir                         \
    --log-file="${dev_home}/pip.log"       \
    "$@";
}


pip_install_from_cache () {
  "${python}" -m pip install                 \
    --disable-pip-version-check              \
    --pre                                    \
    --no-index                               \
    --no-cache-dir                           \
    --find-links="${dev_patches}"            \
    --find-links="${dev_home}/pip_downloads" \
    --log-file="${dev_home}/pip.log"         \
    "$@";
}


pip_download_and_install () {
  "${python}" -m pip install                 \
    --disable-pip-version-check              \
    --pre                                    \
    --no-cache-dir                           \
    --find-links="${dev_patches}"            \
    --log-file="${dev_home}/pip.log"         \
    "$@";
}


#
# Set up for development
#
develop () {
  init_build;
  c_dependencies;
  py_dependencies;
  macos_oracle;
}


develop_clean () {
  init_build;

  # Clean
  rm -rf "${dev_roots}";
  rm -rf "${py_virtualenv}";
}


develop_distclean () {
  init_build;

  # Clean
  rm -rf "${dev_home}";
}
