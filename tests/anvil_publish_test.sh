#!/usr/bin/env bash
# shellcheck disable=SC2016 # the generated Makefiles and wrappers keep their $ for make and bash
# scripts/anvil-publish.sh against stub anvil, podman, git and build-limited, building a small stand-in for vcache
# from a local origin: one whole publish of all five targets with --commit, the layout, the archive rather than the
# working tree, a fedora44 build that sees none of the caller's environment, each container target built and checked in
# its own image, the build record, refusals (a C++ runtime linked dynamically in any target, a target linked against
# another OS's glibc, an unmerged commit, a commit without anvil.toml, a failed el10 build, an el10 binary that does not
# run in the image, an image pin that matches neither digest), the outbox exit, a dry run that runs nothing, and that
# the repository's anvil.toml lists the targets the script builds.
set -uo pipefail

if [[ $(uname -s) != Linux ]]; then
  echo 'SKIP anvil-publish needs Linux (readelf, ldd)'
  exit 0
fi
TOP=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
REAL_GIT=$(command -v git)
# The real compilers, not a ccache masquerade, which the fedora44 build's fixed PATH would not find its way back from.
NO_CCACHE_PATH=$(tr ':' '\n' <<<"$PATH" | grep -v ccache | paste -sd:)
REAL_GCC=$(PATH=$NO_CCACHE_PATH command -v gcc)
REAL_GXX=$(PATH=$NO_CCACHE_PATH command -v g++)
WORK=$(mktemp -d "${TMPDIR:-/tmp}/vcache-anvil-publish-XXXXXX")
trap 'rm -rf "$WORK"' EXIT

PASS=0 FAIL=0
ok() { printf '  PASS %s\n' "$1"; PASS=$((PASS + 1)); }
bad() { printf '  FAIL %s\n' "$1"; FAIL=$((FAIL + 1)); }
check() { if [[ $2 == "$3" ]]; then ok "$1"; else bad "$1 (expected '$3', got '$2')"; fi; }
has() { if grep -q -- "$3" <<<"$2"; then ok "$1"; else bad "$1 (no '$3' in: $2)"; fi; }
hasNot() { if grep -q -- "$3" <<<"$2"; then bad "$1 ('$3' in: $2)"; else ok "$1"; fi; }

# ---- toolchains: gcc wrappers that mark what they built -------------------------------------------------------------

# writeCompilers <dir> <tag> <c name> <c++ name> <hook>: compilers that build with the real ones and mark the binary
# with <tag>. `-dM -E` names the glibc of the image the stub podman ran them in (STUB_IMAGE_GLIBC), else this host's,
# which is fedora44's, as the real compilers name the glibc they link against.
writeCompilers() {
  local dir=$1 tag=$2 cc=$3 cxx=$4 hook=$5
  printf '#!/usr/bin/env bash
[[ " $* " != *" --version "* ]] || { echo "%s (stub %s) 16.0"; exit 0; }
if [[ " $* " == *" -dM "* ]]; then
  cat >/dev/null
  glibc=${STUB_IMAGE_GLIBC:-2.43}
  printf "#define __GLIBC__ %%s\\n#define __GLIBC_MINOR__ %%s\\n" "${glibc%%%%.*}" "${glibc#*.}"
  exit 0
fi
%s
exec %s -DTOOLCHAIN=\\"%s\\" "$@"
' "$cc" "$tag" "$hook" "$REAL_GCC" "$tag" >"$dir/$cc"
  printf '#!/usr/bin/env bash\nexec %s -DTOOLCHAIN=\\"%s\\" "$@"\n' "$REAL_GXX" "$tag" >"$dir/$cxx"
  chmod +x "$dir/$cc" "$dir/$cxx"
}

# The fedora44 gcc-16 logs the environment it ran in to fedora44-build-env.log; the el10 one fails while STUB_EL10_FAIL
# is set (the stub podman passes the caller's environment through).
makeTree() {
  local tag=$1 tree=$WORK/infra.$1 hook=
  mkdir -p "$tree/bin" "$tree/.build-inputs"
  printf '%s\n' "$tag" >"$tree/.infra-os"
  printf 'abc123  -\n' >"$tree/.build-inputs/gcc.sha256"
  if [[ $tag == fedora44 ]]; then
    hook="printf '%s PATH=%s\\n' \"\${STUB_LEAK-unset}\" \"\$PATH\" >>$WORK/fedora44-build-env.log"
  elif [[ $tag == el10 ]]; then
    hook='[[ -z ${STUB_EL10_FAIL:-} ]] || { echo "stub el10 gcc fails" >&2; exit 1; }'
  fi
  writeCompilers "$tree/bin" "$tag" gcc-16 g++-16 "$hook"
  # Older than gcc-16, though it sorts after it as text.
  printf '#!/usr/bin/env bash\necho "gcc-9 is not the newest gcc" >&2\nexit 99\n' >"$tree/bin/gcc-9"
  chmod +x "$tree"/bin/*
}
for tag in fedora44 el10 ubuntu24 amzn2023; do
  makeTree "$tag"
done
# dc-build's devtoolset-11: the stub podman runs this enable script in place of the image's.
mkdir -p "$WORK/devtoolset/bin"
writeCompilers "$WORK/devtoolset/bin" dc-build gcc g++ ''
printf 'export PATH=%s:$PATH\n' "$WORK/devtoolset/bin" >"$WORK/devtoolset/enable"
printf 'ID=fedora\nVERSION_ID=44\n' >"$WORK/os-release"
# Each image's /etc/os-release, as the real images have them.
mkdir -p "$WORK/os-releases"
printf 'ID="almalinux"\nVERSION_ID="10.2"\nPLATFORM_ID="platform:el10"\n' >"$WORK/os-releases/el10"
printf 'ID=ubuntu\nVERSION_ID="24.04"\nID_LIKE=debian\n' >"$WORK/os-releases/ubuntu24"
printf 'ID="amzn"\nVERSION_ID="2023"\nPLATFORM_ID="platform:al2023"\n' >"$WORK/os-releases/amzn2023"
printf 'NAME="CentOS Linux"\nID="centos"\nVERSION_ID="7"\n' >"$WORK/os-releases/centos7"

# ---- stubs -----------------------------------------------------------------------------------------------------------

STUBS=$WORK/stubs
mkdir -p "$STUBS"
cat >"$STUBS/git" <<EOF
#!/usr/bin/env bash
printf '%s\n' "\$*" >>"\$STUB_LOG/git"
exec $REAL_GIT "\$@"
EOF
cat >"$STUBS/build-limited" <<'EOF'
#!/usr/bin/env bash
# One line per call: the build scripts hold newlines.
printf '%s\n' "${*//$'\n'/ }" >>"$STUB_LOG/build-limited"
export HS_BUILD_SLICE=stub.slice
exec "$@"
EOF
# Runs the container's command on this host, with each mount's container path mapped back to its host path, the image's
# glibc in STUB_IMAGE_GLIBC for the stub compilers (STUB_FORCE_GLIBC overrides it in every image), the image's
# os-release in place of /etc/os-release, and the stub devtoolset in place of the dc image's. An image's id is
# stubid-<its name>.
cat >"$STUBS/podman" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "${*//$'\n'/ }" >>"$STUB_LOG/podman"
if [[ $1 == image && $2 == inspect ]]; then
  name=${*: -1}
  printf 'stubid-%s sha256:stubdigest localhost/sun_container@sha256:repodigest \n' "${name//[^a-zA-Z0-9]/-}"
  exit 0
fi
[[ $1 == run ]] || { echo "stub podman: unexpected $1" >&2; exit 99; }
shift
mounts=() envs=() workdir=.
while (($#)); do
  case $1 in
    -v) mounts+=("$2"); shift 2 ;;
    -e) envs+=("$2"); shift 2 ;;
    -w) workdir=$2; shift 2 ;;
    --user) shift 2 ;;
    --rm | --userns=* | --cgroup-parent=*) shift ;;
    -*) echo "stub podman: unknown option $1" >&2; exit 99 ;;
    *) break ;;
  esac
done
case $1 in
  stubid-*sun-container*) export STUB_IMAGE_GLIBC=2.39 os=el10 ;;
  stubid-*ubuntu2404) export STUB_IMAGE_GLIBC=2.39 os=ubuntu24 ;;
  stubid-*al2023) export STUB_IMAGE_GLIBC=2.34 os=amzn2023 ;;
  stubid-*dc-build) export STUB_IMAGE_GLIBC=2.17 os=centos7 ;;
  *) echo "stub podman: ran $1, not an inspected image id" >&2; exit 98 ;;
esac
[[ -z ${STUB_FORCE_GLIBC:-} ]] || STUB_IMAGE_GLIBC=$STUB_FORCE_GLIBC
shift
set -- "${@//\/opt\/rh\/devtoolset-11\/enable/$STUB_DEVTOOLSET_ENABLE}"
set -- "${@//\/etc\/os-release/$STUB_OS_RELEASES/$os}"
if [[ -n ${STUB_PODMAN_FAIL_VERSION:-} && ${*: -1} == --version ]]; then
  echo "stub podman: $1 cannot run in this image" >&2
  exit 126
fi
mapPath() {
  local element mount host target
  for element in "$@"; do
    for mount in "${mounts[@]}"; do
      host=${mount%%:*} target=${mount#*:} target=${target%%:*}
      if [[ $element == "$target" || $element == "$target"/* ]]; then
        element=$host${element#"$target"}
        break
      fi
    done
    printf '%s\0' "$element"
  done
}
mapValue() {
  local IFS=:
  # shellcheck disable=SC2046
  mapPath $1 | tr '\0' '\n' | paste -sd:
}
mapped_envs=()
for assignment in "${envs[@]}"; do
  mapped_envs+=("${assignment%%=*}=$(mapValue "${assignment#*=}")")
done
# NUL-separated, as an argument (a bash -c script) may hold newlines.
mapfile -d '' -t args < <(mapPath "$@")
cd "$(mapPath "$workdir" | tr -d '\0')" && exec env "${mapped_envs[@]}" "${args[@]}"
EOF
cat >"$STUBS/anvil" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >>"$STUB_LOG/anvil"
[[ $1 == publish ]] || { echo "stub anvil: unexpected $1" >&2; exit 99; }
dir=
while (($#)); do
  [[ $1 != --dir ]] || dir=$2
  shift
done
snapshot=$STUB_LOG/publish-$(wc -l <"$STUB_LOG/anvil")
mkdir -p "$snapshot"
cp -a "$dir/." "$snapshot/"
pwd >"$snapshot.cwd"
echo "vcache 1.1 published"
exit "${STUB_ANVIL_EXIT:-0}"
EOF
chmod +x "$STUBS"/*

# ---- a stand-in for vcache, on a local origin --------------------------------------------------------------------

g() { "$REAL_GIT" -C "$1" -c user.name=t -c user.email=t@example.invalid "${@:2}"; }
writeSource() {
  local dir=$1 mark=$2 runtime=$3
  mkdir -p "$dir/third-party"
  printf '#!/bin/sh\necho stub fetch\n' >"$dir/third-party/fetch.sh"
  chmod +x "$dir/third-party/fetch.sh"
  printf '#include <stdio.h>\nint main(void) { printf("vcache 9.9.9 %%s %%s\\n", "%s", TOOLCHAIN); return 0; }\n' \
    "$mark" >"$dir/vcache.c"
  printf 'int vcache_fstrace_marker;\n' >"$dir/fstrace.c"
  if [[ $runtime == el10-dynamic ]]; then
    printf '#include <cstdio>\n#include <string>\nint main() { std::string v = "vcache 9.9.9 %s"; std::puts(v.c_str()); }\n' \
      "$mark" >"$dir/vcache.cc"
    printf 'ifneq ($(findstring infra.el10,$(PATH)),)\nSRC := vcache.cc\nLINK = $(CXX)\nelse\nSRC := vcache.c\nLINK = $(CC)\nendif\nall:\n\tmkdir -p bin\n\t$(LINK) -o bin/vcache $(SRC)\n\t$(CC) -shared -fPIC -o bin/vcache-fstrace.so fstrace.c\n' \
      >"$dir/Makefile"
  elif [[ $runtime == static ]]; then
    printf 'all:\n\tmkdir -p bin\n\t$(CC) -o bin/vcache vcache.c\n\t$(CC) -shared -fPIC -o bin/vcache-fstrace.so fstrace.c\n' \
      >"$dir/Makefile"
  else
    # A C++ binary linking libstdc++ dynamically, as vcache's Makefile does when its compiler has no libstdc++.a.
    printf '#include <cstdio>\n#include <string>\nint main() { std::string v = "vcache 9.9.9 %s"; std::puts(v.c_str()); }\n' \
      "$mark" >"$dir/vcache.cc"
    printf 'all:\n\tmkdir -p bin\n\t$(CXX) -o bin/vcache vcache.cc\n\t$(CC) -shared -fPIC -o bin/vcache-fstrace.so fstrace.c\n' \
      >"$dir/Makefile"
  fi
}
anvilToml() {
  printf '[artifact.vcache]\nmajor = %s\nos = ["fedora44", "el10", "ubuntu24", "amzn2023", "dc-build"]\n' "$1"
}

"$REAL_GIT" init -q --bare "$WORK/origin.git"
"$REAL_GIT" init -q -b fbg "$WORK/seed"
writeSource "$WORK/seed" c1 static
g "$WORK/seed" add -A && g "$WORK/seed" commit -qm c1
C1=$(g "$WORK/seed" rev-parse HEAD)
# An anvil.toml from before the five targets.
printf '[artifact.vcache]\nmajor = 1\nos = ["fedora44", "el10"]\n' >"$WORK/seed/anvil.toml"
g "$WORK/seed" add -A && g "$WORK/seed" commit -qm c1b
C1B=$(g "$WORK/seed" rev-parse HEAD)
writeSource "$WORK/seed" c2 dynamic
anvilToml 1 >"$WORK/seed/anvil.toml"
g "$WORK/seed" add -A && g "$WORK/seed" commit -qm c2
C2=$(g "$WORK/seed" rev-parse HEAD)
g "$WORK/seed" push -q "$WORK/origin.git" fbg
"$REAL_GIT" clone -q -b fbg "$WORK/origin.git" "$WORK/repo"
# A fetch refspec that does not cover fbg, as in a single-branch clone of another branch: only a fetch naming its own
# destination moves origin/fbg.
g "$WORK/repo" config remote.origin.fetch '+refs/heads/other:refs/remotes/origin/other'
# origin's tip moves after the clone, so only a fetch finds it.
writeSource "$WORK/seed" c2b el10-dynamic
g "$WORK/seed" add -A && g "$WORK/seed" commit -qm c2b
C2B=$(g "$WORK/seed" rev-parse HEAD)
g "$WORK/seed" rm -q vcache.cc
writeSource "$WORK/seed" c3 static
g "$WORK/seed" add -A && g "$WORK/seed" commit -qm c3
C3=$(g "$WORK/seed" rev-parse HEAD)
g "$WORK/seed" push -q "$WORK/origin.git" fbg
g "$WORK/repo" fetch -q "$WORK/seed" fbg:side-base
g "$WORK/repo" switch -q -c side side-base
printf '// unmerged\n' >>"$WORK/repo/fstrace.c"
g "$WORK/repo" commit -qam unmerged
SIDE=$(g "$WORK/repo" rev-parse HEAD)
g "$WORK/repo" switch -q fbg
# Uncommitted edits the archive must not see.
writeSource "$WORK/repo" worktree static
anvilToml 7 >"$WORK/repo/anvil.toml"
mkdir -p "$WORK/repo/scripts"
cp "$TOP/scripts/anvil-publish.sh" "$WORK/repo/scripts/"

# runPublish <case> [args...]: runs the script with the stubs, its stdout and stderr in $WORK/<case>.out.
runPublish() {
  local name=$1
  shift
  mkdir -p "$WORK/logs/$name"
  touch "$WORK/logs/$name"/{anvil,podman,git,build-limited}
  STUB_LOG=$WORK/logs/$name PATH="$STUBS:$PATH" VCACHE_BUILD_LIMITED=$STUBS/build-limited \
    STUB_DEVTOOLSET_ENABLE=$WORK/devtoolset/enable STUB_OS_RELEASES=$WORK/os-releases \
    VCACHE_FEDORA44_INFRA=$WORK/infra.fedora44 \
    VCACHE_EL10_INFRA=$WORK/infra.el10 VCACHE_UBUNTU24_INFRA=$WORK/infra.ubuntu24 \
    VCACHE_AMZN2023_INFRA=$WORK/infra.amzn2023 VCACHE_OS_RELEASE=$WORK/os-release VCACHE_PUBLISH_ROOT=$WORK/builds \
    "$WORK/repo/scripts/anvil-publish.sh" "$@" >"$WORK/$name.out" 2>&1
}
log() { cat "$WORK/logs/$1/$2"; }
workDirs() { find "$WORK/builds" -mindepth 1 -maxdepth 1 -name 'anvil-publish-*' 2>/dev/null | wc -l; }

echo 'anvil-publish: origin fbg tip'
STUB_LEAK=leaked runPublish tip
check "exits 0" "$?" 0
check "one anvil call" "$(log tip anvil | wc -l)" 1
check "a whole publish of origin's fbg tip, by full commit" "$(log tip anvil | sed 's/--dir [^ ]*/--dir D/')" \
  "publish vcache --dir D --commit $C3 --color never"
snapshot=$WORK/logs/tip/publish-1
check "the publish directory holds all five targets and anvil.toml" \
  "$(cd "$snapshot" && find . -type f -printf '%P %m\n' | LC_ALL=C sort | paste -sd,)" \
  "amzn2023/vcache 555,amzn2023/vcache-fstrace.so 555,anvil.toml 644,dc-build/vcache 555,dc-build/vcache-fstrace.so 555,el10/vcache 555,el10/vcache-fstrace.so 555,fedora44/vcache 555,fedora44/vcache-fstrace.so 555,ubuntu24/vcache 555,ubuntu24/vcache-fstrace.so 555"
check "fedora44 is the archive's c3, built with the fedora44 tree's newest gcc" \
  "$("$snapshot/fedora44/vcache" --version)" "vcache 9.9.9 c3 fedora44"
for tag in el10 ubuntu24 amzn2023; do
  check "$tag is the archive's c3, built in its image with the $tag tree" \
    "$("$snapshot/$tag/vcache" --version)" "vcache 9.9.9 c3 $tag"
done
check "dc-build is the archive's c3, built in its image with devtoolset-11" \
  "$("$snapshot/dc-build/vcache" --version)" "vcache 9.9.9 c3 dc-build"
check "anvil.toml is the commit's, not the working tree's" "$(cat "$snapshot/anvil.toml")" "$(anvilToml 1)"
check "anvil runs outside the checkout" "$("$REAL_GIT" -C "$(cat "$snapshot.cwd")" rev-parse 2>/dev/null && echo git)" ""
builds=$(log tip podman | grep -- 'fetch\.sh')
check "four container builds" "$(wc -l <<<"$builds")" 4
check "each container build joins build-limited's slice" "$(grep -c -- '--cgroup-parent=stub.slice' <<<"$builds")" 4
has "el10 builds in its image with the el10 tree at /opt/infra" "$builds" \
  "-v $WORK/infra.el10:/opt/infra:ro .* stubid-sun-container-el10 bash"
has "ubuntu24 builds in its image with the ubuntu24 tree at /opt/infra" "$builds" \
  "-v $WORK/infra.ubuntu24:/opt/infra:ro .* stubid-localhost-fbg-dev-ubuntu2404 bash"
has "amzn2023 builds in its image with the amzn2023 tree at /opt/infra" "$builds" \
  "-v $WORK/infra.amzn2023:/opt/infra:ro .* stubid-localhost-fbg-dev-al2023 bash"
has "dc-build builds in its image with devtoolset-11 and no tree" "$builds" \
  "/dc-build -e HOME=/tmp .* stubid-localhost-dc-build bash -c set -e; source /opt/rh/devtoolset-11/enable;"
check "all five builds go through build-limited" "$(log tip build-limited | wc -l)" 5
has "el10 is checked in the image with its tree and no library path" "$(log tip podman)" \
  "run --rm -v [^ ]*/out/el10:/v:ro -v $WORK/infra.el10:/opt/infra:ro stubid-sun-container-el10 /v/vcache --version"
for spec in ubuntu24:localhost-fbg-dev-ubuntu2404 amzn2023:localhost-fbg-dev-al2023 dc-build:localhost-dc-build; do
  has "${spec%%:*} is checked in its image, with no tree" "$(log tip podman)" \
    "run --rm -v [^ ]*/out/${spec%%:*}:/v:ro stubid-${spec#*:} /v/vcache --version"
done
check "git archives c3 once per target" "$(log tip git | grep -c " archive $C3$")" 5
hasNot "git never touches the working tree" "$(log tip git)" ' \(checkout\|switch\|stash\|reset\|restore\|clean\|worktree\)\( \|$\)'
has "the build record names the image digest" "$(cat "$WORK/tip.out")" \
  'el10      sun_container:el10 = digest sha256:stubdigest, id stubid-sun-container-el10'
for tag in fedora44 el10 ubuntu24 amzn2023 dc-build; do
  has "the build record names $tag's compiler, binutils, cmake and glibc" "$(cat "$WORK/tip.out")" \
    "^  $tag *cc: .*; ld: .*; as: .*; cmake: .*; glibc [0-9]"
done
for spec in fedora44:fedora44:2.43 el10:el10:2.39 ubuntu24:ubuntu24:2.39 amzn2023:amzn2023:2.34 dc-build:centos7:2.17; do
  IFS=: read -r tag os glibc <<<"$spec"
  has "the build record names the OS $tag was built on, the glibc it linked against and the versions it needs" \
    "$(cat "$WORK/tip.out")" \
    "^  $tag *built on $os, linked against glibc $glibc; needs GLIBC_[0-9.]* (vcache), GLIBC_[0-9.]* (vcache-fstrace.so)$"
done
check "the fedora44 build sees none of the caller's environment and a fixed PATH" \
  "$(sort -u "$WORK/fedora44-build-env.log")" "unset PATH=/usr/bin:/bin"
check "the work directory is removed" "$(workDirs)" 0

echo 'anvil-publish: --dry-run'
runPublish dry --dry-run
check "exits 0" "$?" 0
has "prints the publish" "$(cat "$WORK/dry.out")" "anvil publish vcache --dir .* --commit $C3"
check "lists five builds" "$(grep '^\[anvil-publish\] build [a-z0-9-]* \(natively\|in\) ' "$WORK/dry.out" |
  sed 's/ with .*//')" \
  "[anvil-publish] build fedora44 natively
[anvil-publish] build el10 in sun_container:el10
[anvil-publish] build ubuntu24 in localhost/fbg-dev:ubuntu2404
[anvil-publish] build amzn2023 in localhost/fbg-dev:al2023
[anvil-publish] build dc-build in localhost/dc:build"
check "calls no anvil" "$(log dry anvil | wc -l)" 0
check "runs no container" "$(log dry podman | grep -c '^run')" 0
check "builds nothing" "$(log dry build-limited | wc -l)" 0
check "archives nothing" "$(log dry git | grep -c ' archive ')" 0
check "makes no work directory" "$(workDirs)" 0

echo 'anvil-publish: a target linking libstdc++ dynamically'
runPublish dynamic --commit "${C2:0:12}"
check "exits 1" "$?" 1
has "names the dynamic C++ runtime" "$(cat "$WORK/dynamic.out")" 'links the C++ runtime dynamically'
check "publishes nothing" "$(log dynamic anvil | wc -l)" 0
check "keeps the work directory" "$(workDirs)" 1

echo 'anvil-publish: a commit that is not on origin/fbg'
runPublish side --commit "$SIDE"
check "exits 1" "$?" 1
has "says why" "$(cat "$WORK/side.out")" 'is not on origin/fbg'
check "publishes nothing" "$(log side anvil | wc -l)" 0

echo 'anvil-publish: a commit without anvil.toml'
runPublish bootstrap --commit "$C1"
check "exits 1" "$?" 1
has "says why" "$(cat "$WORK/bootstrap.out")" 'has no anvil.toml, so it names no major'
check "builds nothing" "$(log bootstrap build-limited | wc -l)" 0
check "publishes nothing" "$(log bootstrap anvil | wc -l)" 0

echo 'anvil-publish: only the el10 target links libstdc++ dynamically'
runPublish el10dynamic --commit "$C2B"
check "exits 1" "$?" 1
has "names the el10 binary" "$(cat "$WORK/el10dynamic.out")" 'out/el10/vcache links the C++ runtime dynamically'
check "publishes nothing" "$(log el10dynamic anvil | wc -l)" 0

echo 'anvil-publish: the el10 build fails'
STUB_EL10_FAIL=1 runPublish el10fails
check "exits non-zero" "$(($? != 0))" 1
check "publishes nothing" "$(log el10fails anvil | wc -l)" 0
has "keeps the work directory" "$(cat "$WORK/el10fails.out")" 'work directory kept'

echo 'anvil-publish: the el10 binary does not run in the image'
STUB_PODMAN_FAIL_VERSION=1 runPublish el10runs
check "exits 1" "$?" 1
has "says which check failed" "$(cat "$WORK/el10runs.out")" 'el10: vcache --version failed'
check "publishes nothing" "$(log el10runs anvil | wc -l)" 0

echo 'anvil-publish: an image pinned by digest'
VCACHE_EL10_IMAGE=localhost/sun_container@sha256:repodigest runPublish pinrepo --dry-run
check "a pin matching a repo digest is accepted" "$?" 0
VCACHE_EL10_IMAGE=localhost/sun_container@sha256:otherdigest runPublish pinother --dry-run
check "a pin matching neither digest is refused" "$?" 1
has "says what the image has" "$(cat "$WORK/pinother.out")" \
  'resolves to digest sha256:stubdigest and repo digests localhost/sun_container@sha256:repodigest'

echo 'anvil-publish: the server is unreachable'
STUB_ANVIL_EXIT=8 runPublish outbox
check "exits 8" "$?" 8
has "says the publish is queued" "$(cat "$WORK/outbox.out")" 'queued the publish in its outbox'

echo 'anvil-publish: a target built in another OS image'
# el10 and ubuntu24 both ship glibc 2.39: only the image's os-release tells them apart.
for spec in EL10:localhost/fbg-dev:ubuntu2404:el10:ubuntu24 UBUNTU24:sun_container:el10:ubuntu24:el10 \
  AMZN2023:localhost/fbg-dev:ubuntu2404:amzn2023:ubuntu24 UBUNTU24:localhost/fbg-dev:al2023:ubuntu24:amzn2023 \
  DC_BUILD:sun_container:el10:dc-build:el10; do
  IFS=: read -r name image_name image_tag tag built <<<"$spec"
  case_name=wrong-$tag-in-$built
  (
    export "VCACHE_${name}_IMAGE=$image_name:$image_tag"
    runPublish "$case_name"
  )
  check "$tag built in $image_name:$image_tag exits 1" "$?" 1
  has "$tag built in $image_name:$image_tag: names the OS it was built on" "$(cat "$WORK/$case_name.out")" \
    "$tag was built on $built (its build's /etc/os-release), not "
  check "$tag built in $image_name:$image_tag: publishes nothing" "$(log "$case_name" anvil | wc -l)" 0
done

echo "anvil-publish: a toolchain linking against another glibc"
STUB_FORCE_GLIBC=2.34 runPublish glibc
check "exits 1" "$?" 1
has "names the glibc and the OS" "$(cat "$WORK/glibc.out")" \
  "el10 was linked against glibc 2.34, but el10 ships glibc 2.39"
check "publishes nothing" "$(log glibc anvil | wc -l)" 0

echo "anvil-publish: a commit whose anvil.toml lists other targets"
runPublish oldtoml --commit "$C1B"
check "exits 1" "$?" 1
has "says what it lists and what is built" "$(cat "$WORK/oldtoml.out")" \
  "anvil.toml lists os = \[el10 fedora44\]; this script builds amzn2023 dc-build el10 fedora44 ubuntu24"
check "builds nothing" "$(log oldtoml build-limited | wc -l)" 0
check "publishes nothing" "$(log oldtoml anvil | wc -l)" 0

echo "anvil-publish: the repository's anvil.toml"
check "lists the targets the script builds, in its order" \
  "$(sed -n 's/^os = \[\(.*\)\]$/\1/p' "$TOP/anvil.toml" | tr ',' '\n' | tr -d '" ')" \
  "$(grep -o 'targets [a-z0-9 -]* (dry run' "$WORK/dry.out" | sed 's/^targets //; s/ (dry run$//' | tr ' ' '\n')"

printf 'anvil-publish: %s passed, %s failed\n' "$PASS" "$FAIL"
((FAIL == 0))
