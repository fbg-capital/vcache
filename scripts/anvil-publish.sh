#!/usr/bin/env bash
# Publishes vcache to anvil: builds the fedora44 and el10 targets of one commit from its archived source and sends both
# in one `anvil publish`, so the version is complete or absent.
#
#   scripts/anvil-publish.sh [--dry-run] [--commit <commit>] [--keep]
#
#   --commit   the commit to build; default origin's fbg tip, fetched first. It must be on origin/fbg, because
#              vcache:1 is the binary every consumer build runs.
#   --dry-run  resolve the commit, the image and the toolchains, then print every step without running any of them.
#   --keep     keep the work directory; it is always kept when a step fails.
#
# The source is `git archive <commit>`, never the working tree, and the commit must carry anvil.toml. fedora44 builds
# natively with the newest gcc-N of the fedora44 toolchain tree, under `env -i` with PATH=/usr/bin:/bin and only the
# variables the build needs; el10 in the el10 image with the el10 tree mounted at /opt/infra; both through build-limited.
# Both trees ship libstdc++.a; the host compiler may not, and then vcache's Makefile links the C++ runtime dynamically.
# Source paths are mapped to `.`, so a target's bytes depend on the source and the toolchain only, and publishing a
# commit again is reported as already published. Before anything is published, each target must link libstdc++ and
# libgcc statically, and run `vcache --version` and load its tracer where it runs: on this host, and in the el10 image
# (with the tree its entry point requires, but none of the tree's libraries on the library path). The build record
# (commit, image digest, toolchains, the ld, as, cmake and glibc each build saw, file hashes) is printed before the publish.
#
# Environment:
#   ANVIL                  the anvil binary (default: anvil on PATH)
#   VCACHE_BUILD_LIMITED   the build wrapper (default ~/bin/build-limited)
#   VCACHE_EL10_IMAGE      the el10 image (default sun_container:el10); <name>@sha256:<digest> pins one, matched against
#                          the image's digest and its repo digests
#   VCACHE_EL10_INFRA      the el10 toolchain tree (default /opt/infra.el10)
#   VCACHE_FEDORA44_INFRA  the fedora44 toolchain tree (default /opt/infra.fedora44)
#   VCACHE_PUBLISH_ROOT    where the work directory is made (default /build/$USER/cache/vcache-build)
#   VCACHE_OS_RELEASE      the os-release file naming this host (default /etc/os-release)
set -euo pipefail

repo=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
anvil=${ANVIL:-anvil}
build_limited=${VCACHE_BUILD_LIMITED:-$HOME/bin/build-limited}
el10_image=${VCACHE_EL10_IMAGE:-sun_container:el10}
el10_infra=${VCACHE_EL10_INFRA:-/opt/infra.el10}
fedora44_infra=${VCACHE_FEDORA44_INFRA:-/opt/infra.fedora44}
publish_root=${VCACHE_PUBLISH_ROOT:-/build/${USER:-$(id -un)}/cache/vcache-build}
os_release=${VCACHE_OS_RELEASE:-/etc/os-release}
# vcache's -O3 LTO Boost.Spirit units take several GB each; build-limited's MAKEFLAGS is sized for the whole slice.
build_jobs=8
fedora44_path=/usr/bin:/bin
branch=fbg
targets=(fedora44 el10)

is_dry_run=0
should_keep=0
commit_arg=
# Set by resolveCommit, describeTree and resolveImage.
commit='' gcc_fedora44='' gcc_el10='' record_fedora44='' record_el10='' el10_image_id='' record_image=''
while (($#)); do
  case $1 in
    --dry-run) is_dry_run=1 ;;
    --keep) should_keep=1 ;;
    --commit)
      commit_arg=${2:?--commit needs a commit}
      shift
      ;;
    --commit=*) commit_arg=${1#*=} ;;
    -h | --help)
      sed -n '2,/^set -euo/{/^set -euo/d;s/^# \{0,1\}//;p}' "${BASH_SOURCE[0]}"
      exit 0
      ;;
    *)
      printf 'anvil-publish: unknown argument %s (see --help)\n' "$1" >&2
      exit 2
      ;;
  esac
  shift
done

die() {
  printf 'anvil-publish: %s\n' "$*" >&2
  exit 1
}

step() {
  printf '[anvil-publish] %s\n' "$*"
}

# Prints the command, then runs it unless this is a dry run.
run() {
  printf '  +'
  printf ' %q' "$@"
  printf '\n'
  ((is_dry_run)) || "$@"
}

# check <description> <command...>: prints the description, then runs the command unless this is a dry run.
check() {
  printf '  + check %s\n' "$1"
  shift
  ((is_dry_run)) || "$@"
}

# The host's OS tag by anvil's rule (devtools scripts/infra-os-tag.sh): el<N> from PLATFORM_ID, else <ID><major>.
getHostTag() (
  ID='' VERSION_ID='' PLATFORM_ID=''
  # shellcheck disable=SC1090
  source "$os_release"
  if [[ $PLATFORM_ID =~ ^platform:el([0-9]+)$ ]]; then
    printf 'el%s\n' "${BASH_REMATCH[1]}"
  else
    printf '%s%s\n' "$ID" "${VERSION_ID%%.*}"
  fi
)

# Sets commit to the full name of --commit, or of origin's fbg tip, and refuses one that is not on origin/fbg.
resolveCommit() {
  local tip
  git -C "$repo" fetch --quiet origin "+refs/heads/$branch:refs/remotes/origin/$branch" ||
    die "cannot fetch $branch from origin in $repo"
  tip=$(git -C "$repo" rev-parse --verify "refs/remotes/origin/$branch^{commit}")
  if [[ -z $commit_arg ]]; then
    commit=$tip
    return 0
  fi
  commit=$(git -C "$repo" rev-parse --verify --quiet --end-of-options "$commit_arg^{commit}") ||
    die "no commit $commit_arg in $repo, even after fetching origin"
  git -C "$repo" merge-base --is-ancestor "$commit" "$tip" ||
    die "$commit is not on origin/$branch; vcache:1 is what every consumer build runs, so only merged commits are published"
  # The major comes from the commit itself, never from a working tree.
  git -C "$repo" cat-file -e "$commit:anvil.toml" 2>/dev/null ||
    die "${commit:0:12} has no anvil.toml, so it names no major; publish a commit that carries it"
}

# getNewestGcc <tree>: the highest N of the tree's bin/gcc-N.
getNewestGcc() {
  find "$1/bin" -maxdepth 1 -name 'gcc-[0-9]*' -printf '%f\n' 2>/dev/null | grep -E '^gcc-[0-9]+$' |
    sed 's/^gcc-//' | sort -n | tail -1 || true
}

# describeTree <tag> <tree>: checks the tree is that OS's toolchain tree, sets gcc_<tag> to its newest gcc-N and
# record_<tag> to what the build record says about it.
describeTree() {
  local tag=$1 tree=$2 infra_os version gcc_inputs=none
  infra_os=$(cat -- "$tree/.infra-os" 2>/dev/null || true)
  [[ $infra_os == "$tag" ]] || die "$tree is not the $tag toolchain tree (.infra-os: ${infra_os:-missing})"
  version=$(getNewestGcc "$tree")
  [[ -n $version ]] || die "no gcc-N in $tree/bin; build the devtools $tag tree first"
  [[ ! -r $tree/.build-inputs/gcc.sha256 ]] || gcc_inputs=$(cut -d' ' -f1 "$tree/.build-inputs/gcc.sha256")
  printf -v "gcc_$tag" '%s' "$version"
  printf -v "record_$tag" '%s (.infra-os %s), %s, gcc inputs %s' "$tree" "$infra_os" \
    "$("$tree/bin/gcc-$version" --version | head -1)" "$gcc_inputs"
}

# Sets el10_image_id to the image's id, which every podman run uses, so a retag during the run changes nothing.
resolveImage() {
  local digest repo_digests pinned
  read -r el10_image_id digest repo_digests < <(podman image inspect \
    --format '{{.Id}} {{.Digest}} {{range .RepoDigests}}{{.}} {{end}}' "$el10_image") ||
    die "no image $el10_image (podman image inspect failed)"
  if [[ $el10_image == *@sha256:* ]]; then
    pinned=${el10_image##*@}
    [[ $digest == "$pinned" || " $repo_digests " == *"@$pinned "* ]] ||
      die "$el10_image resolves to digest $digest and repo digests ${repo_digests:-none}"
  fi
  record_image="$el10_image = digest $digest, id $el10_image_id"
}

# exportSource <dir>: the commit's tree, plus the gperftools download the checkout already has (fetch.sh verifies it).
exportSource() {
  local dir=$1 tarball
  run mkdir -p -- "$dir"
  printf '  + git archive %s | tar -x -C %q\n' "$commit" "$dir"
  if ! ((is_dry_run)); then
    git -C "$repo" archive "$commit" | tar -x -C "$dir"
  fi
  for tarball in "$repo/third-party/.dl/gperftools.tar.gz" "$work/fedora44/third-party/.dl/gperftools.tar.gz"; do
    if [[ -f $tarball && $tarball != "$dir"/* ]]; then
      run mkdir -p -- "$dir/third-party/.dl"
      run cp -- "$tarball" "$dir/third-party/.dl/"
      break
    fi
  done
}

# The fedora44 build's whole environment besides CC, CXX and the flags: nothing comes from the caller's (no ccache
# masquerade, no CPATH or LD_LIBRARY_PATH), and its HOME is the work directory's.
getFedora44Env() {
  printf '%s\n' PATH="$fedora44_path" HOME="$work/home" LC_ALL=C MAKEFLAGS="-j$build_jobs"
}

buildFedora44() {
  local src=$work/fedora44 map build_env
  map="-ffile-prefix-map=$src=."
  step "build fedora44 natively with $fedora44_infra/bin/gcc-$gcc_fedora44"
  exportSource "$src"
  run mkdir -p -- "$work/home"
  mapfile -t build_env < <(getFedora44Env)
  run "$build_limited" env -i -C "$src" "${build_env[@]}" \
    CC="$fedora44_infra/bin/gcc-$gcc_fedora44 $map" CXX="$fedora44_infra/bin/g++-$gcc_fedora44 $map" \
    CFLAGS="$map" CXXFLAGS="$map" bash -c "set -e; ./third-party/fetch.sh && make -j$build_jobs all"
}

# The el10 build's environment inside the image, besides CC, CXX and the flags.
getEl10Env() {
  printf '%s\n' -e "MAKEFLAGS=-j$build_jobs" -e HOME=/tmp -e PATH=/opt/infra/bin:/usr/local/bin:/usr/bin:/bin \
    -e LD_LIBRARY_PATH=/opt/infra/lib64:/opt/infra/lib
}

# The container joins build-limited's slice, which build-limited names in HS_BUILD_SLICE.
buildEl10() {
  local src=$work/el10 map build_env
  map="-ffile-prefix-map=$src=."
  step "build el10 in $el10_image with $el10_infra/bin/gcc-$gcc_el10 at /opt/infra"
  exportSource "$src"
  mapfile -t build_env < <(getEl10Env)
  # shellcheck disable=SC2016 # expanded inside build-limited, which sets HS_BUILD_SLICE
  run "$build_limited" bash -c 'exec podman run ${HS_BUILD_SLICE:+--cgroup-parent="$HS_BUILD_SLICE"} "$@"' podman-run \
    --rm --userns=keep-id --user "$(id -u):$(id -g)" -v "$src:$src:Z" -v "$el10_infra:/opt/infra:ro" -w "$src" \
    "${build_env[@]}" -e "CC=gcc-$gcc_el10 $map" -e "CXX=g++-$gcc_el10 $map" -e "CFLAGS=$map" -e "CXXFLAGS=$map" \
    "$el10_image_id" bash -c "set -e; ./third-party/fetch.sh && make -j$build_jobs all"
}

layOut() {
  local tag
  step "lay out $out/<target>/{vcache,vcache-fstrace.so}"
  for tag in "${targets[@]}"; do
    run install -D -m 555 -- "$work/$tag/bin/vcache" "$out/$tag/vcache"
    run install -D -m 555 -- "$work/$tag/bin/vcache-fstrace.so" "$out/$tag/vcache-fstrace.so"
  done
  # The commit's anvil.toml beside the targets gives the major. The work directory is outside git, so anvil sends --commit
  # as given instead of resolving it in a checkout, whose HEAD and tracked changes have nothing to do with the archive.
  run cp -- "$work/fedora44/anvil.toml" "$out/anvil.toml"
}

# targetDir <tag>: where the target's files are, as seen by targetRun.
targetDir() {
  if [[ $1 == el10 ]]; then
    printf '/v\n'
  else
    printf '%s\n' "$out/$1"
  fi
}

# targetRun <tag> [<name>=<value>...] <command...>: runs the command where the target runs: on this host, or in the
# el10 image with the el10 tree at /opt/infra, which its entry point requires. sunbird's container builds also put the
# tree's lib64 on LD_LIBRARY_PATH; this leaves it off, so only the image's own libraries resolve.
targetRun() {
  local tag=$1 podman_args=()
  shift
  if [[ $tag != el10 ]]; then
    env "$@"
    return
  fi
  while [[ $1 == *=* ]]; do
    podman_args+=(-e "$1")
    shift
  done
  podman run --rm -v "$out/el10:/v:ro" -v "$el10_infra:/opt/infra:ro" "${podman_args[@]}" "$el10_image_id" "$@"
}

# checkNeeded <file>: the C++ runtime is inside the binary; the consumers' hosts and images need not have one.
checkNeeded() {
  local needed
  needed=$(readelf -d -- "$1" | sed -n 's/.*(NEEDED).*\[\(.*\)\]$/\1/p')
  if grep -Eq '^(libstdc\+\+|libgcc_s)\.so' <<<"$needed"; then
    die "$1 links the C++ runtime dynamically (NEEDED: $(paste -sd' ' <<<"$needed")); its toolchain has no libstdc++.a"
  fi
}

# checkRuns <tag>: every library resolves where the target runs, vcache answers --version, and its tracer loads with
# every symbol bound.
checkRuns() {
  local tag=$1 dir output
  dir=$(targetDir "$tag")
  output=$(targetRun "$tag" ldd "$dir/vcache" 2>&1) || die "$tag: ldd $dir/vcache failed: $output"
  if grep -q 'not found' <<<"$output"; then
    die "$tag: ldd $dir/vcache: $(grep 'not found' <<<"$output" | paste -sd';')"
  fi
  output=$(targetRun "$tag" "$dir/vcache" --version 2>&1) || die "$tag: vcache --version failed: $output"
  [[ ${output%%$'\n'*} =~ ^vcache\ [0-9] ]] || die "$tag: vcache --version printed: ${output%%$'\n'*}"
  printf '    %s: %s\n' "$tag" "${output%%$'\n'*}"
  output=$(targetRun "$tag" LD_BIND_NOW=1 LD_PRELOAD="$dir/vcache-fstrace.so" /bin/true 2>&1) ||
    die "$tag: vcache-fstrace.so does not load: $output"
  [[ -z $output ]] || die "$tag: vcache-fstrace.so does not load: $output"
}

checkTargets() {
  local tag file
  step "check each target holds its C++ runtime and runs where it will run"
  for tag in "${targets[@]}"; do
    for file in vcache vcache-fstrace.so; do
      check "readelf -d $out/$tag/$file names no libstdc++ or libgcc_s" checkNeeded "$out/$tag/$file"
    done
  done
  check "fedora44 on this host: ldd, vcache --version, LD_BIND_NOW=1 LD_PRELOAD=vcache-fstrace.so /bin/true" \
    checkRuns fedora44
  check "el10 in $el10_image_id with $el10_infra at /opt/infra and no LD_LIBRARY_PATH: the same three" checkRuns el10
}

# Prints the first --version line of ld, as and cmake, and the glibc version, as the environment running it finds them.
# shellcheck disable=SC2016 # runs in the build's environment
tools_probe='for tool in ld as cmake; do printf "%s: %s; " "$tool" "$("$tool" --version 2>&1 | head -1)"; done
  printf "glibc %s\n" "$(ldd --version 2>&1 | head -1 | sed "s/.* //")"'

printRecord() {
  local tag file build_env tools_fedora44 tools_el10
  step "build record"
  printf '  commit    %s\n' "$commit"
  printf '  fedora44  native on %s, %s\n' "$host_tag" "$record_fedora44"
  printf '  el10      %s; %s\n' "$record_image" "$record_el10"
  ((is_dry_run)) && return 0
  mapfile -t build_env < <(getFedora44Env)
  tools_fedora44=$(env -i "${build_env[@]}" bash -c "$tools_probe") || die "cannot probe the fedora44 build's tools"
  mapfile -t build_env < <(getEl10Env)
  tools_el10=$(podman run --rm -v "$el10_infra:/opt/infra:ro" "${build_env[@]}" "$el10_image_id" bash -c "$tools_probe") ||
    die "cannot probe the el10 build's tools"
  printf '  fedora44  %s\n' "$tools_fedora44"
  printf '  el10      %s\n' "$tools_el10"
  for tag in "${targets[@]}"; do
    for file in vcache vcache-fstrace.so; do
      printf '  %-8s  %s  %s\n' "$tag" "$(sha256sum <"$out/$tag/$file" | cut -d' ' -f1)" "$file"
    done
  done
}

publish() {
  local status=0
  step "publish ${targets[*]} as one version of vcache"
  # From the work directory, outside git, so no checkout's anvil.toml is found above it.
  run env -C "$work" "$anvil" publish vcache --dir "$out" --commit "$commit" --color never || status=$?
  case $status in
    0) ;;
    8)
      printf 'anvil-publish: the server is unreachable; anvil queued the publish in its outbox (exit 8) and the next anvil command sends it (anvil outbox lists it)\n' >&2
      exit 8
      ;;
    *)
      printf 'anvil-publish: anvil publish failed (exit %s)\n' "$status" >&2
      exit "$status"
      ;;
  esac
}

cleanUp() {
  local status=$?
  [[ -n ${work:-} && -d $work ]] || return 0
  if ((status == 0 && !should_keep)); then
    rm -rf -- "$work"
  else
    printf 'anvil-publish: work directory kept: %s\n' "$work" >&2
  fi
}

host_tag=$(getHostTag)
[[ $host_tag == fedora44 ]] || die "the fedora44 target is built natively, and this host is $host_tag"
command -v "$anvil" >/dev/null || die "no anvil binary ($anvil); install it or set ANVIL"
[[ -x $build_limited ]] || die "no build wrapper at $build_limited; set VCACHE_BUILD_LIMITED"
resolveCommit
describeTree fedora44 "$fedora44_infra"
describeTree el10 "$el10_infra"
resolveImage
if ((is_dry_run)); then
  step "vcache ${commit:0:12} -> anvil, targets ${targets[*]} (dry run: nothing below runs)"
  work="$publish_root/anvil-publish-${commit:0:12}.XXXXXX"
  printf '  + mktemp -d %q\n' "$work"
else
  step "vcache ${commit:0:12} -> anvil, targets ${targets[*]}"
  mkdir -p -- "$publish_root"
  work=$(mktemp -d "$publish_root/anvil-publish-${commit:0:12}.XXXXXX")
  trap cleanUp EXIT
fi
out=$work/out
buildFedora44
buildEl10
layOut
checkTargets
printRecord
publish
