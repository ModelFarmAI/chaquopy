#/bin/bash
set -eu

start_dir=$(pwd)

crystax=${1:?"Usage: build.sh path/to/crystax-ndk"}
crystax_python=$crystax/sources/python

short_ver=2.7
full_ver=2.7.10-1  # See "build number" in PythonPlugin.groovy
target_dir="$start_dir/com/chaquo/python/target/$full_ver"
rm -rf "$target_dir"
mkdir -p "$target_dir"

for abi in armeabi-v7a x86; do
    echo "$abi"
    zipfile="$target_dir/target-$full_ver-$abi.zip"
    rm -f $zipfile
    rm -rf tmp
    mkdir tmp
    cd tmp

    jniLibs_dir="jniLibs/$abi"
    mkdir -p "$jniLibs_dir"
    cp -a "$crystax_python/$short_ver/libs/$abi/libpython$short_ver.so" "$jniLibs_dir"
    cp -a "$crystax/sources/crystax/libs/$abi/libcrystax.so" "$jniLibs_dir"

    mkdir lib-dynload
    dynload_dir="lib-dynload/$abi"
    cp -a "$crystax_python/$short_ver/libs/$abi/modules" "$dynload_dir"
    rm "$dynload_dir/_sqlite3.so"  # TODO 5160

    zip -q -r "$zipfile" *
    cd ..
    rm -r tmp
done

echo "stdlib"
cp "$crystax_python/$short_ver/libs/x86/stdlib.zip" "$target_dir/target-$full_ver-stdlib.zip"

for f in $(find -name *zip); do
    sha1sum "$f" |cut -d' ' -f1 > "$f.sha1";
done