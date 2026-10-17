#!/usr/bin/env bash
# shellcheck disable=SC2016 # the generated Makefiles and wrappers keep their $ for make and bash
# scripts/anvil-publish.sh against stub anvil, podman, git and build-limited, building a small stand-in for vcache
# from a local origin: one whole publish with --commit, the layout, the archive rather than the working tree, the
# static C++ runtime refusal, refusal of an unmerged commit, the outbox exit, and a dry run that runs nothing.
set -uo pipefail

if [[ $(uname -s) != Linux ]]; then
  echo 'SKIP anvil-publish needs Linux (readelf, ldd)'
  exit 0
fi
TOP=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
REAL_GIT=$(command -v git)
REAL_GCC=$(command -v gcc)
REAL_GXX=$(command -v g++)
WORK=$(mktemp -d "${TMPDIR:-/tmp}/vcache-anvil-publish-XXXXXX")
trap 'rm -rf "$WORK"' EXIT

PASS=0 FAIL=0
ok() { printf '  PASS %s\n' "$1"; PASS=$((PASS + 1)); }
bad() { printf '  FAIL %s\n' "$1"; FAIL=$((FAIL + 1)); }
check() { if [[ $2 == "$3" ]]; then ok "$1"; else bad "$1 (expected '$3', got '$2')"; fi; }
has() { if grep -q -- "$3" <<<"$2"; then ok "$1"; else bad "$1 (no '$3' in: $2)"; fi; }
hasNot() { if grep -q -- "$3" <<<"$2"; then bad "$1 ('$3' in: $2)"; else ok "$1"; fi; }

# ---- toolchain trees: gcc-N wrappers that mark what they built ------------------------------------------------------

makeTree() {
  local tag=$1 tree=$WORK/infra.$1
  mkdir -p "$tree/bin" "$tree/.build-inputs"
  printf '%s\n' "$tag" >"$tree/.infra-os"
  printf 'abc123  -\n' >"$tree/.build-inputs/gcc.sha256"
  printf '#!/usr/bin/env bash\n[[ ${1:-} != --version ]] || { echo "gcc-16 (stub %s) 16.0"; exit 0; }\nexec %s -DTOOLCHAIN=\\"%s\\" "$@"\n' \
    "$tag" "$REAL_GCC" "$tag" >"$tree/bin/gcc-16"
  printf '#!/usr/bin/env bash\nexec %s -DTOOLCHAIN=\\"%s\\" "$@"\n' "$REAL_GXX" "$tag" >"$tree/bin/g++-16"
  # Older than gcc-16, though it sorts after it as text.
  printf '#!/usr/bin/env bash\necho "gcc-9 is not the newest gcc" >&2\nexit 99\n' >"$tree/bin/gcc-9"
  chmod +x "$tree"/bin/*
}
makeTree fedora44
makeTree el10
printf 'ID=fedora\nVERSION_ID=44\n' >"$WORK/os-release"

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
printf '%s\n' "$*" >>"$STUB_LOG/build-limited"
export HS_BUILD_SLICE=stub.slice
exec "$@"
EOF
# Runs the container's command on this host, with each mount's container path mapped back to its host path.
cat >"$STUBS/podman" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >>"$STUB_LOG/podman"
if [[ $1 == image && $2 == inspect ]]; then
  echo "stubimageid sha256:stubdigest"
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
[[ $1 == stubimageid ]] || { echo "stub podman: ran $1, not the inspected image id" >&2; exit 98; }
shift
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
    printf '%s\n' "$element"
  done
}
mapValue() {
  local IFS=:
  # shellcheck disable=SC2046
  mapPath $1 | paste -sd:
}
mapped_envs=()
for assignment in "${envs[@]}"; do
  mapped_envs+=("${assignment%%=*}=$(mapValue "${assignment#*=}")")
done
mapfile -t args < <(mapPath "$@")
cd "$(mapPath "$workdir")" && exec env "${mapped_envs[@]}" "${args[@]}"
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
  if [[ $runtime == static ]]; then
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
anvilToml() { printf '[artifact.vcache]\nmajor = %s\nos = ["fedora44", "el10"]\n' "$1"; }

"$REAL_GIT" init -q --bare "$WORK/origin.git"
"$REAL_GIT" init -q -b fbg "$WORK/seed"
writeSource "$WORK/seed" c1 static
g "$WORK/seed" add -A && g "$WORK/seed" commit -qm c1
C1=$(g "$WORK/seed" rev-parse HEAD)
writeSource "$WORK/seed" c2 dynamic
anvilToml 1 >"$WORK/seed/anvil.toml"
g "$WORK/seed" add -A && g "$WORK/seed" commit -qm c2
C2=$(g "$WORK/seed" rev-parse HEAD)
g "$WORK/seed" push -q "$WORK/origin.git" fbg
"$REAL_GIT" clone -q -b fbg "$WORK/origin.git" "$WORK/repo"
# origin's tip moves after the clone, so only a fetch finds it.
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
    VCACHE_FEDORA44_INFRA=$WORK/infra.fedora44 VCACHE_EL10_INFRA=$WORK/infra.el10 VCACHE_OS_RELEASE=$WORK/os-release \
    VCACHE_PUBLISH_ROOT=$WORK/builds "$WORK/repo/scripts/anvil-publish.sh" "$@" >"$WORK/$name.out" 2>&1
}
log() { cat "$WORK/logs/$1/$2"; }
workDirs() { find "$WORK/builds" -mindepth 1 -maxdepth 1 -name 'anvil-publish-*' 2>/dev/null | wc -l; }

echo 'anvil-publish: origin fbg tip'
runPublish tip
check "exits 0" "$?" 0
check "one anvil call" "$(log tip anvil | wc -l)" 1
check "a whole publish of origin's fbg tip, by full commit" "$(log tip anvil | sed 's/--dir [^ ]*/--dir D/')" \
  "publish vcache --dir D --commit $C3 --color never"
snapshot=$WORK/logs/tip/publish-1
check "the publish directory holds both targets and anvil.toml" \
  "$(cd "$snapshot" && find . -type f -printf '%P %m\n' | sort | paste -sd,)" \
  "anvil.toml 644,el10/vcache 555,el10/vcache-fstrace.so 555,fedora44/vcache 555,fedora44/vcache-fstrace.so 555"
check "fedora44 is the archive's c3, built with the fedora44 tree's newest gcc" \
  "$("$snapshot/fedora44/vcache" --version)" "vcache 9.9.9 c3 fedora44"
check "el10 is the archive's c3, built in the image with the el10 tree" \
  "$("$snapshot/el10/vcache" --version)" "vcache 9.9.9 c3 el10"
check "anvil.toml is the commit's, not the working tree's" "$(cat "$snapshot/anvil.toml")" "$(anvilToml 1)"
check "anvil runs outside the checkout" "$("$REAL_GIT" -C "$(cat "$snapshot.cwd")" rev-parse 2>/dev/null && echo git)" ""
has "the el10 build joins build-limited's slice" "$(log tip podman | grep -- ' -w ')" '--cgroup-parent=stub.slice'
has "the el10 build mounts the el10 tree at /opt/infra" "$(log tip podman | grep -- ' -w ')" \
  "-v $WORK/infra.el10:/opt/infra:ro"
check "both builds go through build-limited" "$(log tip build-limited | wc -l)" 2
has "el10 is checked in the image with its tree and no library path" "$(log tip podman)" \
  "run --rm -v [^ ]*/out/el10:/v:ro -v $WORK/infra.el10:/opt/infra:ro stubimageid /v/vcache --version"
check "git archives c3 once per target" "$(log tip git | grep -c " archive $C3$")" 2
hasNot "git never touches the working tree" "$(log tip git)" ' \(checkout\|switch\|stash\|reset\|restore\|clean\|worktree\)\( \|$\)'
has "the build record names the image digest" "$(cat "$WORK/tip.out")" 'digest sha256:stubdigest, id stubimageid'
check "the work directory is removed" "$(workDirs)" 0

echo 'anvil-publish: --dry-run'
runPublish dry --dry-run
check "exits 0" "$?" 0
has "prints the publish" "$(cat "$WORK/dry.out")" "anvil publish vcache --dir .* --commit $C3"
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
check "exits 0" "$?" 0
has "says where anvil.toml comes from" "$(cat "$WORK/bootstrap.out")" 'has no anvil.toml; using'
check "takes the checkout's anvil.toml" "$(cat "$WORK/logs/bootstrap/publish-1/anvil.toml")" "$(anvilToml 7)"

echo 'anvil-publish: the server is unreachable'
STUB_ANVIL_EXIT=8 runPublish outbox
check "exits 8" "$?" 8
has "says the publish is queued" "$(cat "$WORK/outbox.out")" 'queued the publish in its outbox'

printf 'anvil-publish: %s passed, %s failed\n' "$PASS" "$FAIL"
((FAIL == 0))
