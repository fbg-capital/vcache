#!/usr/bin/env bash
# Publishes vcache to anvil: builds every target of one commit from its archived source, each with its own OS's glibc
# and toolchain, and sends them all in one `anvil publish`, so the version is complete or absent.
#
#   scripts/anvil-publish.sh [--dry-run] [--commit <commit>] [--keep]
#
#   --commit   the commit to build; default origin's fbg tip, fetched first. It must be on origin/fbg, because
#              vcache:1 is the binary every consumer build runs.
#   --dry-run  resolve the commit, the images and the toolchains, then print every step without running any of them.
#   --keep     keep the work directory; it is always kept when a step fails.
#
# The source is `git archive <commit>`, never the working tree, and the commit must carry anvil.toml. The targets, one
# per OS the consumers build on, each built through build-limited:
#
#   fedora44  natively with the newest gcc-N of the fedora44 toolchain tree, under `env -i` with PATH=/usr/bin:/bin and
#             only the variables the build needs
#   el10      in the el10 image with the el10 tree mounted at /opt/infra
#   ubuntu24  in host_setup's fbg-dev:ubuntu2404 image with the ubuntu24 tree mounted at /opt/infra
#   amzn2023  in host_setup's fbg-dev:al2023 image with the amzn2023 tree mounted at /opt/infra
#   dc-build  in the dc container's image (CentOS 7) with its devtoolset-11 gcc, as devtools built it for that container
#
# Each tree and devtoolset-11 ship libstdc++.a; a compiler without one makes vcache's Makefile link the C++ runtime
# dynamically. Source paths are mapped to `.`, so a target's bytes depend on the source and the toolchain only, and
# publishing a commit again is reported as already published. Before anything is published, each target must link
# libstdc++ and libgcc statically, must have been linked against its OS's glibc (target_glibc below; a target built in
# another OS's image fails here), and must run `vcache --version` and load its tracer where it runs: fedora44 on this
# host, every other target in its image (el10 with the tree its entry point requires; none with a tree's libraries on
# the library path). The build record (commit, image digests, toolchains, the compiler, ld, as, cmake and glibc each
# build saw, the newest glibc symbol version each file needs, file hashes) is printed before the publish.
#
# Environment:
#   ANVIL                  the anvil binary (default: anvil on PATH)
#   VCACHE_BUILD_LIMITED   the build wrapper (default ~/bin/build-limited)
#   VCACHE_<TAG>_IMAGE     the image of EL10 (default sun_container:el10), UBUNTU24 (localhost/fbg-dev:ubuntu2404),
#                          AMZN2023 (localhost/fbg-dev:al2023) or DC_BUILD (localhost/dc:build); <name>@sha256:<digest>
#                          pins one, matched against the image's digest and its repo digests
#   VCACHE_<TAG>_INFRA     the toolchain tree of FEDORA44, EL10, UBUNTU24 or AMZN2023 (default /opt/infra.<tag>)
#   VCACHE_PUBLISH_ROOT    where the work directory is made (default /build/$USER/cache/vcache-build)
#   VCACHE_OS_RELEASE      the os-release file naming this host (default /etc/os-release)
set -euo pipefail

repo=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
anvil=${ANVIL:-anvil}
build_limited=${VCACHE_BUILD_LIMITED:-$HOME/bin/build-limited}
publish_root=${VCACHE_PUBLISH_ROOT:-/build/${USER:-$(id -un)}/cache/vcache-build}
os_release=${VCACHE_OS_RELEASE:-/etc/os-release}
# vcache's -O3 LTO Boost.Spirit units take several GB each; build-limited's MAKEFLAGS is sized for the whole slice.
build_jobs=8
branch=fbg
targets=(fedora44 el10 ubuntu24 amzn2023 dc-build)
# The glibc each target's OS release ships. A release never moves to another glibc, so a build that saw any other one
# ran in the wrong image.
declare -A target_glibc=([fedora44]=2.43 [el10]=2.39 [ubuntu24]=2.39 [amzn2023]=2.34 [dc-build]=2.17)
declare -A target_image=(
  [el10]=${VCACHE_EL10_IMAGE:-sun_container:el10}
  [ubuntu24]=${VCACHE_UBUNTU24_IMAGE:-localhost/fbg-dev:ubuntu2404}
  [amzn2023]=${VCACHE_AMZN2023_IMAGE:-localhost/fbg-dev:al2023}
  [dc-build]=${VCACHE_DC_BUILD_IMAGE:-localhost/dc:build}
)
declare -A target_infra=(
  [fedora44]=${VCACHE_FEDORA44_INFRA:-/opt/infra.fedora44}
  [el10]=${VCACHE_EL10_INFRA:-/opt/infra.el10}
  [ubuntu24]=${VCACHE_UBUNTU24_INFRA:-/opt/infra.ubuntu24}
  [amzn2023]=${VCACHE_AMZN2023_INFRA:-/opt/infra.amzn2023}
)
# The image's system gcc 4.8 lacks C++20.
dc_build_enable=/opt/rh/devtoolset-11/enable

is_dry_run=0
should_keep=0
commit_arg=
# Set by resolveCommit, describeTree and resolveImage.
commit=''
declare -A gcc_version=() toolchain_record=() image_id=() image_record=()
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

# describeToolchain <tag>: checks a tree is that OS's toolchain tree and sets gcc_version[<tag>] to its newest gcc-N;
# sets toolchain_record[<tag>] to what the build record says about the toolchain.
describeToolchain() {
  local tag=$1 tree=${target_infra[$1]-} infra_os version gcc_inputs=none
  if [[ -z $tree ]]; then
    toolchain_record[$tag]="the image's $dc_build_enable"
    return 0
  fi
  infra_os=$(cat -- "$tree/.infra-os" 2>/dev/null || true)
  [[ $infra_os == "$tag" ]] || die "$tree is not the $tag toolchain tree (.infra-os: ${infra_os:-missing})"
  version=$(getNewestGcc "$tree")
  [[ -n $version ]] || die "no gcc-N in $tree/bin; build the devtools $tag tree first"
  [[ ! -r $tree/.build-inputs/gcc.sha256 ]] || gcc_inputs=$(cut -d' ' -f1 "$tree/.build-inputs/gcc.sha256")
  gcc_version[$tag]=$version
  toolchain_record[$tag]="$tree (.infra-os $infra_os), $("$tree/bin/gcc-$version" --version | head -1), gcc inputs $gcc_inputs"
}

# resolveImage <tag>: sets image_id[<tag>] to the image's id, which every podman run uses, so a retag during the run
# changes nothing, and image_record[<tag>] to its name, digest and id.
resolveImage() {
  local tag=$1 image=${target_image[$1]} id digest repo_digests pinned
  read -r id digest repo_digests < <(podman image inspect \
    --format '{{.Id}} {{.Digest}} {{range .RepoDigests}}{{.}} {{end}}' "$image") ||
    die "no image $image for $tag (podman image inspect failed)"
  if [[ $image == *@sha256:* ]]; then
    pinned=${image##*@}
    [[ $digest == "$pinned" || " $repo_digests " == *"@$pinned "* ]] ||
      die "$image resolves to digest $digest and repo digests ${repo_digests:-none}"
  fi
  image_id[$tag]=$id
  image_record[$tag]="$image = digest $digest, id $id"
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

# getBuildEnv <tag>: the build's whole environment besides what its image sets, one NAME=value per line. Natively
# nothing comes from the caller's (no ccache masquerade, no CPATH or LD_LIBRARY_PATH) and HOME is the work directory's.
getBuildEnv() {
  local tag=$1 map="-ffile-prefix-map=$work/$1=." cc cxx
  case $tag in
    fedora44)
      printf '%s\n' PATH=/usr/bin:/bin HOME="$work/home" LC_ALL=C
      cc=${target_infra[$tag]}/bin/gcc-${gcc_version[$tag]} cxx=${target_infra[$tag]}/bin/g++-${gcc_version[$tag]}
      ;;
    dc-build)
      printf '%s\n' HOME=/tmp
      cc=gcc cxx=g++
      ;;
    *)
      printf '%s\n' HOME=/tmp PATH=/opt/infra/bin:/usr/local/bin:/usr/bin:/bin LD_LIBRARY_PATH=/opt/infra/lib64:/opt/infra/lib
      cc=gcc-${gcc_version[$tag]} cxx=g++-${gcc_version[$tag]}
      ;;
  esac
  printf '%s\n' "MAKEFLAGS=-j$build_jobs" "CC=$cc $map" "CXX=$cxx $map" "CFLAGS=$map" "CXXFLAGS=$map"
}

# setBuildCommand <tag> <script>: sets build_command to the command that runs the bash script in the target's build
# environment and source directory. A container joins the slice HS_BUILD_SLICE names, which build-limited sets.
setBuildCommand() {
  local tag=$1 script=$2 src=$work/$1 assignment build_env podman_args
  mapfile -t build_env < <(getBuildEnv "$tag")
  if [[ $tag == fedora44 ]]; then
    build_command=(env -i -C "$src" "${build_env[@]}" bash -c "set -e; $script")
    return 0
  fi
  podman_args=(--rm -v "$src:$src:Z" -w "$src")
  if [[ $tag == dc-build ]]; then
    # Root in a rootless container writes as the caller, as in the dc container's own builds.
    script="source $dc_build_enable; $script"
  else
    podman_args+=(--userns=keep-id --user "$(id -u):$(id -g)" -v "${target_infra[$tag]}:/opt/infra:ro")
  fi
  for assignment in "${build_env[@]}"; do
    podman_args+=(-e "$assignment")
  done
  # shellcheck disable=SC2016 # expanded by the bash that runs podman
  build_command=(bash -c 'exec podman run ${HS_BUILD_SLICE:+--cgroup-parent="$HS_BUILD_SLICE"} "$@"' podman-run
    "${podman_args[@]}" "${image_id[$tag]}" bash -c "set -e; $script")
}

# Writes the glibc the build's compiler links against, <major>.<minor>, to vcache-build-glibc.
# shellcheck disable=SC2016 # runs in the build's environment
glibc_probe='printf "#include <features.h>\n" | $CC -dM -E -x c - |
  awk '\''$2 == "__GLIBC__" { major = $3 } $2 == "__GLIBC_MINOR__" { minor = $3 } END { print major "." minor }'\'' \
  >vcache-build-glibc'

buildTarget() {
  local tag=$1
  if [[ $tag == fedora44 ]]; then
    step "build fedora44 natively with ${target_infra[$tag]}/bin/gcc-${gcc_version[$tag]}"
  elif [[ $tag == dc-build ]]; then
    step "build dc-build in ${target_image[$tag]} with $dc_build_enable"
  else
    step "build $tag in ${target_image[$tag]} with ${target_infra[$tag]}/bin/gcc-${gcc_version[$tag]} at /opt/infra"
  fi
  exportSource "$work/$tag"
  [[ $tag != fedora44 ]] || run mkdir -p -- "$work/home"
  setBuildCommand "$tag" "./third-party/fetch.sh && make -j$build_jobs all && $glibc_probe"
  run "$build_limited" "${build_command[@]}"
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
  if [[ $1 == fedora44 ]]; then
    printf '%s\n' "$out/$1"
  else
    printf '/v\n'
  fi
}

# targetRun <tag> [<name>=<value>...] <command...>: runs the command where the target runs: fedora44 on this host, any
# other in its image. The el10 image's entry point requires the el10 tree at /opt/infra; sunbird's container builds also
# put the tree's lib64 on LD_LIBRARY_PATH, which this leaves off, so only the image's own libraries resolve.
targetRun() {
  local tag=$1 podman_args=()
  shift
  if [[ $tag == fedora44 ]]; then
    env "$@"
    return
  fi
  podman_args=(--rm -v "$out/$tag:/v:ro")
  [[ $tag != el10 ]] || podman_args+=(-v "${target_infra[$tag]}:/opt/infra:ro")
  while [[ $1 == *=* ]]; do
    podman_args+=(-e "$1")
    shift
  done
  podman run "${podman_args[@]}" "${image_id[$tag]}" "$@"
}

# checkNeeded <file>: the C++ runtime is inside the binary; the consumers' hosts and images need not have one.
checkNeeded() {
  local needed
  needed=$(readelf -d -- "$1" | sed -n 's/.*(NEEDED).*\[\(.*\)\]$/\1/p')
  if grep -Eq '^(libstdc\+\+|libgcc_s)\.so' <<<"$needed"; then
    die "$1 links the C++ runtime dynamically (NEEDED: $(paste -sd' ' <<<"$needed")); its toolchain has no libstdc++.a"
  fi
}

# checkGlibc <tag>: the build linked against the glibc of the target's OS.
checkGlibc() {
  local tag=$1 built
  built=$(cat -- "$work/$tag/vcache-build-glibc" 2>/dev/null || true)
  [[ $built == "${target_glibc[$tag]}" ]] ||
    die "$tag was linked against glibc ${built:-unknown}, but $tag ships glibc ${target_glibc[$tag]}; it was built in another OS's image"
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
  local tag file where
  step "check each target holds its C++ runtime, was linked against its OS's glibc, and runs where it will run"
  for tag in "${targets[@]}"; do
    for file in vcache vcache-fstrace.so; do
      check "readelf -d $out/$tag/$file names no libstdc++ or libgcc_s" checkNeeded "$out/$tag/$file"
    done
    check "$tag was linked against glibc ${target_glibc[$tag]}" checkGlibc "$tag"
  done
  for tag in "${targets[@]}"; do
    case $tag in
      fedora44) where="on this host" ;;
      el10) where="in ${image_id[$tag]} with ${target_infra[$tag]} at /opt/infra and no LD_LIBRARY_PATH" ;;
      *) where="in ${image_id[$tag]}" ;;
    esac
    check "$tag $where: ldd, vcache --version, LD_BIND_NOW=1 LD_PRELOAD=vcache-fstrace.so /bin/true" checkRuns "$tag"
  done
}

# getNeededGlibc <file>: the newest GLIBC_ symbol version the file needs.
getNeededGlibc() {
  readelf -V -W -- "$1" | sed -n 's/.*Name: GLIBC_\([0-9.]*\).*/\1/p' | sort -V | tail -1
}

# Prints the first --version line of the compiler, ld, as and cmake, and the glibc version, as the build's environment
# finds them.
# shellcheck disable=SC2016 # runs in the build's environment
tools_probe='printf "cc: %s; " "$($CC --version 2>&1 | head -1)"
  for tool in ld as cmake; do printf "%s: %s; " "$tool" "$("$tool" --version 2>&1 | head -1)"; done
  printf "glibc %s\n" "$(ldd --version 2>&1 | head -1 | sed "s/.* //")"'

printRecord() {
  local tag file tools
  step "build record"
  printf '  commit    %s\n' "$commit"
  for tag in "${targets[@]}"; do
    if [[ $tag == fedora44 ]]; then
      printf '  %-8s  native on %s, %s\n' "$tag" "$host_tag" "${toolchain_record[$tag]}"
    else
      printf '  %-8s  %s; %s\n' "$tag" "${image_record[$tag]}" "${toolchain_record[$tag]}"
    fi
  done
  ((is_dry_run)) && return 0
  for tag in "${targets[@]}"; do
    setBuildCommand "$tag" "$tools_probe"
    tools=$("${build_command[@]}") || die "cannot probe the $tag build's tools"
    printf '  %-8s  %s\n' "$tag" "$tools"
  done
  for tag in "${targets[@]}"; do
    printf '  %-8s  linked against glibc %s; needs GLIBC_%s (vcache), GLIBC_%s (vcache-fstrace.so)\n' "$tag" \
      "$(cat -- "$work/$tag/vcache-build-glibc")" "$(getNeededGlibc "$out/$tag/vcache")" \
      "$(getNeededGlibc "$out/$tag/vcache-fstrace.so")"
  done
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
for tag in "${targets[@]}"; do
  describeToolchain "$tag"
  [[ $tag == fedora44 ]] || resolveImage "$tag"
done
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
for tag in "${targets[@]}"; do
  buildTarget "$tag"
done
layOut
checkTargets
printRecord
publish
