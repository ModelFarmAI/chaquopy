#!/usr/bin/env python3
#
# This script requires Python 3.6 or later, and the requirements listed in requirements.txt.

import argparse
from copy import deepcopy
import csv
from dataclasses import dataclass, field
from email import generator, message, parser
from glob import glob
import jsonschema
import multiprocessing
import os
from os.path import abspath, basename, dirname, exists, isdir, join
import pkg_resources
import re
import shlex
import subprocess
import sys
import tempfile
from textwrap import dedent

from elftools.elf.elffile import ELFFile
import jinja2
from wheel.archive import archive_wheelfile
from wheel.bdist_wheel import bdist_wheel
import yaml


PROGRAM_NAME = basename(__file__)
PYPI_DIR = abspath(dirname(__file__))

PYTHON_VERSION = "3.7"
PYTHON_SUFFIX = PYTHON_VERSION + "m"

# All libraries are listed under their SONAMEs.
STANDARD_LIBS = {
    # Android-provided libraries up to API level 15
    # (https://developer.android.com/ndk/guides/stable_apis).
    "libandroid.so", "libc.so", "libdl.so", "libEGL.so", "libGLESv1_CM.so", "libGLESv2.so",
    "libjnigraphics.so", "liblog.so", "libm.so", "libOpenMAXAL.so", "libOpenSLES.so",
    "libz.so",

    # Chaquopy-provided libraries (libpythonX.Y.so is added below)
    "libcrypto.so.1.0.0", "libsqlite3.so.0", "libssl.so.1.0.0",
}

# TODO: break out the build script fragments which get the actual version numbers from the
# toolchain, and call them here.
COMPILER_LIBS = {
    "libgfortran.so.3": ("chaquopy-libgfortran", "4.9"),
    "libc++_shared.so": ("chaquopy-libcxx", "7000"),
}


@dataclass
class Abi:
    name: str                               # Android ABI name.
    tool_prefix: str                        # GCC target triplet.
    cflags: str = field(default="")
    ldflags: str = field(default="")

ABIS = {abi.name: abi for abi in [
    Abi("armeabi-v7a", "arm-linux-androideabi",
        cflags="-march=armv7-a -mfloat-abi=softfp -mfpu=vfpv3-d16 -mthumb",  # See standalone
        ldflags="-march=armv7-a -Wl,--fix-cortex-a8"),                       # toolchain docs.
    Abi("arm64-v8a", "aarch64-linux-android"),
    Abi("x86", "i686-linux-android"),
    Abi("x86_64", "x86_64-linux-android"),
]}


class BuildWheel:

    def main(self):
        try:
            self.parse_args()
            self.package_dir = self.find_package(self.package)

            self.meta = self.load_meta(self.package)
            self.package = self.meta["package"]["name"]
            self.version = str(self.meta["package"]["version"])  # YAML may parse it as a number.
            self.dist_info = (f"{normalize_name_wheel(self.package)}-"
                              f"{normalize_version(self.version)}.dist-info")

            self.needs_cmake = False
            if "cmake" in self.meta["requirements"]["build"]:
                self.meta["requirements"]["build"].remove("cmake")
                self.needs_cmake = True

            self.needs_python = (self.meta["source"] == "pypi")
            self.bundled_reqs = []
            for name in ["openssl", "python", "sqlite"]:
                if name in self.meta["requirements"]["host"]:
                    self.meta["requirements"]["host"].remove(name)
                    if name == "python":
                        self.needs_python = True
                    else:
                        # OpenSSL and SQLite currently work without any build flags, but it's
                        # worth keeping them in existing meta.yaml files in case that changes.
                        self.bundled_reqs.append(name)

            self.unpack_and_build()

        except CommandError as e:
            log("Error: " + str(e))
            sys.exit(1)

    def unpack_and_build(self):
        if self.needs_python:
            self.find_python()
        else:
            self.compat_tag = f"py{PYTHON_VERSION[0]}-none-{self.platform_tag}"

        build_reqs = self.get_requirements("build")
        if build_reqs:
            run(f"{self.pip} install{' -v' if self.verbose else ''} " +
                " ".join(f"{name}=={version}" for name, version in build_reqs))

        self.version_dir = f"{self.package_dir}/build/{self.version}"
        ensure_dir(self.version_dir)
        cd(self.version_dir)
        self.build_dir = f"{self.version_dir}/{self.compat_tag}"

        if self.no_unpack:
            log("Skipping download and unpack due to --no-unpack")
            assert_isdir(self.build_dir)
        else:
            ensure_empty(self.build_dir)
            self.unpack_source()
            self.apply_patches()

        if self.no_build:
            log("Skipping build due to --no-build")
        else:
            self.reqs_dir = f"{self.build_dir}/requirements"
            if self.no_reqs:
                log("Skipping requirements extraction due to --no-reqs")
            else:
                self.extract_requirements()
            self.update_env()
            cd(f"{self.build_dir}/src")
            wheel_filename = self.build_wheel()
            return self.fix_wheel(wheel_filename)

    def parse_args(self):
        ap = argparse.ArgumentParser(add_help=False)
        ap.add_argument("--help", action="help", help=argparse.SUPPRESS)
        ap.add_argument("-v", "--verbose", action="store_true", help="Log more detail")

        skip_group = ap.add_mutually_exclusive_group()
        skip_group.add_argument("--no-unpack", action="store_true", help="Skip download and unpack "
                                "(an existing build/.../src directory must exist, and will be "
                                "reused)")
        skip_group.add_argument("--no-build", action="store_true", help="Download and unpack, but "
                                "skip build")

        ap.add_argument("--no-reqs", action="store_true", help="Skip extracting requirements "
                        "(any existing build/.../requirements directory will be reused)")
        ap.add_argument("--extra-packages", metavar="DIR", default=[], action="append",
                        help="Extra directory to search for package information, in addition "
                        "to packages/ in the same directory as this script. Can be used "
                        "multiple times.")
        ap.add_argument("--toolchain", metavar="DIR", type=abspath, required=True,
                        help="Path to toolchain")
        ap.add_argument("package", help="Name of package to build")
        ap.parse_args(namespace=self)

        self.setup_toolchain()
        self.platform_tag = f"android_{self.api_level}_{self.abi.replace('-', '_')}"

    def find_python(self):
        self.python_include_dir = f"{self.toolchain}/sysroot/usr/include/python{PYTHON_SUFFIX}"
        assert_isdir(self.python_include_dir)
        python_lib = f"{self.toolchain}/sysroot/usr/lib/libpython{PYTHON_SUFFIX}.so"
        assert_exists(python_lib)
        STANDARD_LIBS.add(basename(python_lib) + ".1.0")

        # We require the build and target Python versions to be the same, because
        # many setup.py scripts are affected by sys.version.
        self.pip = f"python{PYTHON_VERSION} -m pip --disable-pip-version-check"

        self.compat_tag = (f"cp{PYTHON_VERSION.replace('.', '')}-"
                           f"cp{PYTHON_SUFFIX.replace('.', '')}-"
                           f"{self.platform_tag}")

    def unpack_source(self):
        src_dir = f"{self.build_dir}/src"
        source = self.meta["source"]
        if not source:
            ensure_dir(src_dir)
        elif "git_url" in source:
            # Unfortunately --depth doesn't apply to submodules, and --shallow-submodules
            # doesn't work either (https://github.com/rust-lang/rust/issues/34228).
            run(f"git clone -b {source['git_rev']} --depth 1 --recurse-submodules "
                f"{source['git_url']} {src_dir}")
        elif "path" in source:
            abs_path = abspath(join(self.package_dir, source["path"]))
            run(f"cp -a {abs_path} {src_dir}")
        else:
            source_filename = (self.download_pypi() if source == "pypi"
                               else self.download_url(source["url"]))
            temp_dir = tempfile.mkdtemp(prefix="build-wheel-")
            if source_filename.endswith("zip"):
                run(f"unzip -d {temp_dir} -q {source_filename}")
            else:
                run(f"tar -C {temp_dir} -xf {source_filename}")

            files = os.listdir(temp_dir)
            if len(files) == 1 and isdir(f"{temp_dir}/{files[0]}"):
                run(f"mv {temp_dir}/{files[0]} {src_dir}")
                run(f"rm -rf {temp_dir}")
            else:
                run(f"mv {temp_dir} {src_dir}")

            # This is pip's equivalent to our requirements mechanism.
            if exists(f"{src_dir}/pyproject.toml"):
                run(f"rm {src_dir}/pyproject.toml")

    def download_pypi(self):
        sdist_filename = self.find_sdist()
        if sdist_filename:
            log("Using existing source archive")
        else:
            result = run(f"{self.pip} download{' -v' if self.verbose else ''} --no-deps "
                         f"--no-binary {self.package} --no-build-isolation "
                         f"{self.package}=={self.version}", check=False)
            if result.returncode:
                # Even with --no-deps, `pip download` still pointlessly runs egg_info on the
                # downloaded sdist, which installs anything in `setup_requires`
                # (https://github.com/pypa/pip/issues/1884). If this fails or takes a long
                # time, we can't work around it by patching the package, because we haven't had
                # a chance to apply the patches yet.
                warn(f"pip download returned exit status {result.returncode}")
            sdist_filename = self.find_sdist()
            if not sdist_filename:
                raise CommandError("Can't find downloaded source archive. Does the name and "
                                   "version in the package's meta.yaml match the filename "
                                   "shown above?")
        return sdist_filename

    def find_sdist(self):
        for ext in ["zip", "tar.gz", "tgz", "tar.bz2", "tbz2", "tar.xz", "txz"]:
            filename = f"{self.package}-{self.version}.{ext}"
            if exists(filename):
                return filename

    def download_url(self, url):
        source_filename = url[url.rfind("/") + 1:]
        if exists(source_filename):
            log("Using existing source archive")
        else:
            run(f"wget {url}")
        return source_filename

    def apply_patches(self):
        patches_dir = f"{self.package_dir}/patches"
        if exists(patches_dir):
            cd(f"{self.build_dir}/src")
            for patch_filename in os.listdir(patches_dir):
                run(f"patch -p1 -i {patches_dir}/{patch_filename}")

    def build_wheel(self):
        os.environ.update({
            "CHAQUOPY_ABI": self.abi,
            "CHAQUOPY_PYTHON": PYTHON_VERSION,
            "CPU_COUNT": str(multiprocessing.cpu_count())  # Conda variable name.
        })
        build_script = f"{self.package_dir}/build.sh"
        if exists(build_script):
            return self.build_with_script(build_script)
        elif self.needs_python:
            return self.build_with_pip()
        else:
            raise CommandError("Don't know how to build: no build.sh exists, and this is not "
                               "declared as a Python package. Do you need to add a `host` "
                               "requirement of `python`? See meta-schema.yaml.")

    def extract_requirements(self):
        ensure_empty(self.reqs_dir)
        reqs = self.get_requirements("host")
        if not reqs:
            return

        for package, version in reqs:
            dist_dir = f"{PYPI_DIR}/dist/{normalize_name_pypi(package)}"
            matches = []
            for filename in os.listdir(dist_dir):
                match = re.search(fr"^{normalize_name_wheel(package)}-"
                                  fr"{normalize_version(version)}-(?P<build_num>\d+)-"
                                  fr"({self.compat_tag}|"
                                  fr"py{PYTHON_VERSION[0]}-none-{self.platform_tag})"
                                  fr"\.whl$", filename)
                if match:
                    matches.append(match)
            if not matches:
                raise CommandError(f"Couldn't find wheel for requirement {package} {version}")
            matches.sort(key=lambda match: int(match.group("build_num")))
            wheel_filename = join(dist_dir, matches[-1].group(0))
            run(f"unzip -d {self.reqs_dir} -q {wheel_filename}")

            # Put headers on the include path (used by gevent to build against greenlet).
            include_src = f"{self.reqs_dir}/{package}-{version}.data/headers"
            if exists(include_src):
                include_tgt = f"{self.reqs_dir}/chaquopy/include/{package}"
                run(f"mkdir -p {dirname(include_tgt)}")
                run(f"mv {include_src} {include_tgt}")

        # There is an extension to allow ZIP files to contain symlnks, but the zipfile module
        # doesn't support it, and the links wouldn't survive on Windows anyway. So our library
        # wheels include external shared libraries only under their SONAMEs, and we need to
        # create links from the other names so the compiler can find them.
        SONAME_PATTERNS = [(r"^(lib.*)\.so\..*$", r"\1.so"),
                           (r"^(lib.*?)\d+\.so$", r"\1.so"),
                           (r"^(lib.*)_chaquopy\.so$", r"\1.so")]
        reqs_lib_dir = f"{self.reqs_dir}/chaquopy/lib"
        if exists(reqs_lib_dir):
            for filename in os.listdir(reqs_lib_dir):
                for pattern, repl in SONAME_PATTERNS:
                    link_filename = re.sub(pattern, repl, filename)
                    if link_filename != filename:
                        run(f"ln -s {filename} {reqs_lib_dir}/{link_filename}")

    def build_with_script(self, build_script):
        prefix_dir = f"{self.build_dir}/prefix"
        ensure_empty(prefix_dir)
        os.environ.update({  # Conda variable names.
            "PKG_NAME": self.package,
            "PKG_VERSION": self.version,
            "RECIPE_DIR": self.package_dir,
            "SRC_DIR": os.getcwd(),
            "PREFIX": ensure_dir(f"{prefix_dir}/chaquopy"),
        })
        run(build_script)

        # Most scripts will only output into {prefix_dir}/chaquopy, but the TensorFlow script
        # generates a complete wheel hierarchy.
        info_dir = f"{prefix_dir}/{self.dist_info}"
        ensure_dir(info_dir)
        update_message_file(f"{info_dir}/WHEEL",
                            {"Wheel-Version": "1.0",
                             "Root-Is-Purelib": "false"},
                            replace=False)
        update_message_file(f"{info_dir}/METADATA",
                            {"Metadata-Version": "1.2",
                             "Name": self.package,
                             "Version": self.version,
                             "Summary": "",        # Compulsory according to PEP 345,
                             "Download-URL": ""},  #
                            replace=False)

        return self.package_wheel(prefix_dir, os.getcwd())

    def build_with_pip(self):
        # We can't run "setup.py bdist_wheel" directly, because that would only work with
        # setuptools-aware setup.py files. We pass -v unconditionally, because we always want
        # to see the build process output.
        #
        # --no-clean doesn't currently work (https://github.com/pypa/pip/issues/5661). If this
        # impedes debugging a build failure, you can temporarily disable the clean command by
        # adding cmdclass={'clean': object} to setup().
        run(f"{self.pip} wheel -v --no-deps "
            f"--no-clean --build-option --keep-temp "  # Doesn't work: see above.
            f"-e .")
        wheel_filename, = glob("*.whl")  # Note comma
        return abspath(wheel_filename)

    # The environment variables set in this function are used for native builds by
    # distutils.sysconfig.customize_compiler. To make builds as consistent as possible, we
    # define values for all environment variables used by distutils in any supported Python
    # version. We also define some common variables like LD and STRIP which aren't used
    # by distutils, but might be used by custom build scripts.
    def update_env(self):
        env = {}

        env["PYTHONPATH"] = join(PYPI_DIR, "build-packages")
        existing_python_path = os.environ.get("PYTHONPATH")
        if existing_python_path:
            env["PYTHONPATH"] += os.pathsep + existing_python_path

        abi = ABIS[self.abi]
        for tool in ["ar", "as", ("cc", "gcc"), ("cxx", "g++"),
                     ("fc", "gfortran"),   # Used by openblas
                     ("f77", "gfortran"), ("f90", "gfortran"),  # Used by numpy.distutils
                     "ld", "nm", "ranlib", "readelf", "strip"]:
            var, suffix = (tool, tool) if isinstance(tool, str) else tool
            filename = f"{self.toolchain}/bin/{abi.tool_prefix}-{suffix}"
            if suffix != "gfortran":  # Only required for SciPy and OpenBLAS.
                assert_exists(filename)
            env[var.upper()] = filename
        env["LDSHARED"] = f"{env['CC']} -shared"

        gcc_flags = " ".join([
            "-fPIC",  # See standalone toolchain docs, and note below about -pie
            abi.cflags])
        env["CFLAGS"] = gcc_flags
        env["FARCH"] = gcc_flags  # Used by numpy.distutils Fortran compilation.

        # Not including -pie despite recommendation in standalone toolchain docs, because it
        # causes the linker to forget it's supposed to be building a shared library
        # (https://lists.debian.org/debian-devel/2016/05/msg00302.html). It can be added
        # manually for packages which require it (e.g. hdf5).
        env["LDFLAGS"] = " ".join([
            # This flag often catches errors in .so files which would otherwise be delayed
            # until runtime. (Some of the more complex build.sh scripts need to remove this, or
            # use it more selectively.)
            #
            # I tried also adding -Werror=implicit-function-declaration to CFLAGS, but that
            # breaks too many things (e.g. `has_function` in distutils.ccompiler).
            "-Wl,--no-undefined",

            abi.ldflags])

        reqs_prefix = f"{self.reqs_dir}/chaquopy"
        if exists(reqs_prefix):
            env["CFLAGS"] += f" -I{reqs_prefix}/include"
            env["LDFLAGS"] += (f" -L{reqs_prefix}/lib"
                               f" -Wl,--rpath-link,{reqs_prefix}/lib")

        env["ARFLAGS"] = "rc"

        # Set all unused overridable variables to the empty string to prevent the host Python
        # values (if any) from taking effect.
        for var in ["CPPFLAGS", "CXXFLAGS"]:
            env[var] = ""

        # Use -idirafter so that package-specified -I directories take priority (e.g. in grpcio
        # and typed-ast).
        if self.needs_python:
            env["CFLAGS"] += f" -idirafter {self.python_include_dir}"
            env["LDFLAGS"] += f" -lpython{PYTHON_SUFFIX}"

        if self.verbose:
            log("Environment set as follows:\n" +
                "\n".join(f"export {name}='{env[name]}'" for name in sorted(env.keys())))
        os.environ.update(env)

        if self.needs_cmake:
            self.generate_cmake_toolchain()

    # Define the minimum necessary to keep CMake happy. To avoid duplication, we still want to
    # configure as much as possible via update_env.
    def generate_cmake_toolchain(self):
        # See build/cmake/android.toolchain.cmake in the Google NDK.
        CMAKE_PROCESSORS = {
            "armeabi-v7a": "armv7-a",
            "arm64-v8a": "aarch64",
            "x86": "i686",
            "x86_64": "x86_64",
        }

        toolchain_filename = join(self.build_dir, "chaquopy.toolchain.cmake")
        log(f"Generating {toolchain_filename}")
        with open(toolchain_filename, "w") as toolchain_file:
            print(dedent(f"""\
                set(ANDROID TRUE)
                set(CMAKE_ANDROID_STANDALONE_TOOLCHAIN {self.toolchain})

                set(CMAKE_SYSTEM_NAME Android)
                set(CMAKE_SYSTEM_VERSION {self.api_level})
                set(CMAKE_SYSTEM_PROCESSOR {CMAKE_PROCESSORS[self.abi]})

                # Our requirements dir comes before the sysroot, because the sysroot include
                # directory contains headers for third-party libraries like libjpeg which may
                # be of different versions to what we want to use.
                set(CMAKE_FIND_ROOT_PATH {self.reqs_dir}/chaquopy {self.toolchain}/sysroot)
                set(CMAKE_FIND_ROOT_PATH_MODE_PROGRAM NEVER)
                set(CMAKE_FIND_ROOT_PATH_MODE_LIBRARY ONLY)
                set(CMAKE_FIND_ROOT_PATH_MODE_INCLUDE ONLY)
                set(CMAKE_FIND_ROOT_PATH_MODE_PACKAGE ONLY)
                """), file=toolchain_file)

            if self.needs_python:
                print(dedent(f"""\
                    # Variables used by pybind11
                    SET(PYTHONLIBS_FOUND TRUE)
                    SET(PYTHON_LIBRARIES
                        {self.toolchain}/sysroot/usr/lib/libpython{PYTHON_SUFFIX}.so)
                    SET(PYTHON_INCLUDE_DIRS {self.python_include_dir})
                    SET(PYTHON_MODULE_EXTENSION .so)
                    """), file=toolchain_file)

    def setup_toolchain(self):
        for abi in ABIS.values():
            if abi.tool_prefix in os.listdir(self.toolchain):
                break
        else:
            raise CommandError(f"Failed to detect ABI of toolchain {self.toolchain}")
        self.abi = abi.name

        found_target = False
        for word in open(f"{self.toolchain}/bin/clang").read().split():
            if word == "-target":
                found_target = True
            elif found_target:
                self.api_level = int(word[-2:])
                break
        else:
            raise CommandError(f"Failed to detect API level of toolchain {self.toolchain}")

        # On Android, these libraries are incorporated into libc. Create empty .a files so we
        # don't have to patch everything that links against them.
        sysroot_lib = f"{self.toolchain}/sysroot/usr/lib"
        for name in ["pthread", "rt"]:
            if not any(exists(f"{sysroot_lib}/lib{name}.{ext}") for ext in ["a", "so"]):
                run(f"{self.toolchain}/bin/{abi.tool_prefix}-ar r {sysroot_lib}/lib{name}.a")

    def fix_wheel(self, in_filename):
        pure_match = re.search(r"^(.+?)-(.+?)-(.+-none-any)\.whl$", basename(in_filename))
        if pure_match:
            is_pure = True
            self.compat_tag = pure_match.group(3)
        else:
            is_pure = False

        tmp_dir = f"{self.build_dir}/fix_wheel"
        ensure_empty(tmp_dir)
        run(f"unzip -d {tmp_dir} -q {in_filename}")
        info_dir = f"{tmp_dir}/{self.dist_info}"

        log("Updating WHEEL file")
        info_wheel = read_message(f"{info_dir}/WHEEL")
        update_message(info_wheel, {"Generator": PROGRAM_NAME,
                                    "Build": str(self.meta["build"]["number"]),
                                    "Tag": expand_compat_tag(self.compat_tag)})
        write_message(info_wheel, f"{info_dir}/WHEEL")

        available_libs = set(STANDARD_LIBS)
        for libs_dir in [f"{self.reqs_dir}/chaquopy/lib", f"{tmp_dir}/chaquopy/lib"]:
            if exists(libs_dir):
                available_libs.update(os.listdir(libs_dir))

        reqs = set()
        if not is_pure:
            log("Processing native libraries")
            for original_path, _, _ in csv.reader(open(f"{info_dir}/RECORD")):
                if re.search(r"\.(so(\..*)?|a)$", original_path):
                    # Because distutils doesn't propertly support cross-compilation, native
                    # modules will be tagged with the build platform, e.g.
                    # `foo.cpython-36m-x86_64-linux-gnu.so`. Remove these tags.
                    original_path = join(tmp_dir, original_path)
                    fixed_path = re.sub(fr"\.cpython[^.]+\.so$", ".so", original_path)
                    if fixed_path != original_path:
                        run(f"mv {original_path} {fixed_path}")
                    if fixed_path.endswith(".so"):
                        reqs.update(self.check_requirements(fixed_path, available_libs))

                    # https://www.technovelty.org/linux/stripping-shared-libraries.html
                    run(f"chmod +w {fixed_path}")
                    run(f"{os.environ['STRIP']} --strip-unneeded {fixed_path}")

        reqs.update(self.get_requirements("host"))
        if reqs:
            log(f"Adding extra requirements: {reqs}")
            info_metadata = read_message(f"{info_dir}/METADATA")
            update_message(info_metadata, {"Requires-Dist": [f"{package} (>={version})"
                                                             for package, version in reqs]},
                           replace=False)
            write_message(info_metadata, f"{info_dir}/METADATA")

            # Remove the optional JSON copy to save us from having to update it too.
            info_metadata_json = f"{info_dir}/metadata.json"
            if exists(info_metadata_json):
                run(f"rm {info_metadata_json}")

        out_dir = ensure_dir(f"{PYPI_DIR}/dist/{normalize_name_pypi(self.package)}")
        out_filename = self.package_wheel(tmp_dir, out_dir)
        log(f"Wrote {out_filename}")
        return out_filename

    def package_wheel(self, in_dir, out_dir):
        info_dir = f"{in_dir}/{self.dist_info}"
        bdist_wheel.write_record(None, in_dir, info_dir)
        return archive_wheelfile(
            "-".join([
                f"{out_dir}/{normalize_name_wheel(self.package)}",
                normalize_version(self.version),
                str(self.meta["build"]["number"]),
                self.compat_tag]),
            in_dir)

    def check_requirements(self, filename, available_libs):
        reqs = []
        ef = ELFFile(open(filename, "rb"))
        dynamic = ef.get_section_by_name(".dynamic")
        if not dynamic:
            raise CommandError(f"{filename} has no .dynamic section")
        for tag in dynamic.iter_tags():
            if tag.entry.d_tag == "DT_NEEDED":
                req = COMPILER_LIBS.get(tag.needed)
                if req:
                    reqs.append(req)
                elif tag.needed in available_libs:
                    pass
                else:
                    raise CommandError(f"{filename} is linked against unknown library "
                                       f"'{tag.needed}'.")
        return reqs

    def get_requirements(self, req_type):
        reqs = []
        for req in self.meta["requirements"][req_type]:
            package, version = req.split()
            reqs.append((package, version))
        return reqs

    def load_meta(self, package):
        # http://python-jsonschema.readthedocs.io/en/latest/faq/
        def with_defaults(validator_cls):
            def set_defaults(validator, properties, instance, schema):
                for name, subschema in properties.items():
                    if "default" in subschema:
                        instance.setdefault(name, deepcopy(subschema["default"]))
                yield from validator_cls.VALIDATORS["properties"](
                    validator, properties, instance, schema)

            return jsonschema.validators.extend(validator_cls, {"properties": set_defaults})

        # Work around https://github.com/Julian/jsonschema/issues/367 by not enabling defaults
        # during meta-schema validation.
        Validator = jsonschema.Draft4Validator
        schema = yaml.safe_load(open(f"{PYPI_DIR}/meta-schema.yaml"))
        Validator.check_schema(schema)
        meta_str = jinja2.Template(open(f"{self.find_package(package)}/meta.yaml").read()).render()
        meta = yaml.safe_load(meta_str)
        with_defaults(Validator)(schema).validate(meta)
        return meta

    def find_package(self, name):
        name = normalize_name_pypi(name)
        packages_dirs = [f"{PYPI_DIR}/packages"] + self.extra_packages
        for packages_dir in packages_dirs:
            package_dir = join(packages_dir, name)
            if exists(package_dir):
                return package_dir
        else:
            raise CommandError(f"Couldn't find '{name}' in {packages_dirs}")


def update_message_file(filename, d, replace=True):
    try:
        msg = read_message(filename)
    except FileNotFoundError:
        msg = message.Message()
    update_message(msg, d, replace)
    write_message(msg, filename)
    return msg

def read_message(filename):
    return parser.Parser().parse(open(filename))

def update_message(msg, d, replace=True):
    for key, values in d.items():
        if replace:
            del msg[key]  # Removes all lines with this key.
        if not isinstance(values, list):
            values = [values]
        for value in values:
            # __setitem__ doesn't overwrite existing lines here.
            msg[key] = value

def write_message(msg, filename):
    # I don't know whether maxheaderlen is required, but it's used by bdist_wheel.
    generator.Generator(open(filename, "w"), maxheaderlen=0).flatten(msg)


# See PEP 503.
def normalize_name_pypi(name):
    return re.sub(r"[-_.]+", "-", name).lower()

# This is what bdist_wheel does both for wheel filenames and .dist-info directory names.
# NOTE: this is not entirely equivalent to the specifications in PEP 427 and PEP 376.
def normalize_name_wheel(name):
    return re.sub(r"[^A-Za-z0-9.]+", '_', name)

#  e.g. "2017.01.02" -> "2017.1.2"
def normalize_version(version):
    return str(pkg_resources.parse_version(version))


def expand_compat_tag(compat_tag):
    result = []
    impl_tags, abi_tags, plat_tags = compat_tag.split("-")
    for impl in impl_tags.split('.'):
        for abi in abi_tags.split('.'):
            for plat in plat_tags.split('.'):
                result.append('-'.join((impl, abi, plat)))
    return result


def run(command, check=True):
    log(command)
    try:
        return subprocess.run(shlex.split(command), check=check)
    except subprocess.CalledProcessError as e:
        raise CommandError(f"Command returned exit status {e.returncode}")


def ensure_empty(dir_name):
    if exists(dir_name):
        run(f"rm -rf {dir_name}")
    return ensure_dir(dir_name)

def ensure_dir(dir_name):
    if not exists(dir_name):
        run(f"mkdir -p {dir_name}")
    return dir_name

def assert_isdir(filename):
    assert_exists(filename)
    if not isdir(filename):
        raise CommandError(f"{filename} is not a directory")
    return filename

def assert_exists(filename):
    if not exists(filename):
        raise CommandError(f"{filename} does not exist")


def cd(new_dir):
    if new_dir != os.getcwd():
        log(f"cd {new_dir}")
        os.chdir(new_dir)


def warn(s):
    log(f"Warning: {s}")

def log(s):
    print(f"{PROGRAM_NAME}: {s}")
    sys.stdout.flush()


class CommandError(Exception):
    pass


if __name__ == "__main__":
    BuildWheel().main()
