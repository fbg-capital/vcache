// SPDX-FileCopyrightText: 2026 Unto Labs
// SPDX-License-Identifier: Apache-2.0
#include "core/link_trace.h"

#include <fcntl.h>
#include <signal.h>
#include <sys/stat.h>
#include <unistd.h>
#if defined(__linux__)
#include <sys/vfs.h>
#endif

#include <algorithm>
#include <cstdlib>
#include <cerrno>
#include <cstring>
#include <optional>

#include "trace/format.h"
#include "util/fs.h"
#include "util/log.h"
#include "util/str.h"

namespace vcache::core {
namespace {

bool HasPrefix(const std::string& p, std::string_view prefix) {
  return util::StartsWith(p, prefix);
}

bool AppendTraceRecord(std::string_view line, TraceRecords* out) {
  if (line.size() < 3 || line[1] != '\t' || line[2] != '/') return false;
  std::string path(line.substr(2));
  switch (line[0]) {
    case 'R': out->reads.push_back(std::move(path)); break;
    case 'M': out->absent.push_back(std::move(path)); break;
    case 'P': out->tools.push_back(std::move(path)); break;
    case 'L': out->loaded.push_back(std::move(path)); break;
    case 'W': out->written.push_back(std::move(path)); break;
    case 'D': break;
    default: return false;
  }
  return true;
}

bool ZeroTracePadding(std::string_view bytes) {
  return std::all_of(bytes.begin(), bytes.end(), [](char byte) { return byte == 0; });
}

bool ReadMappedTrace(std::string_view bytes, TraceRecords* out, std::string* reason) {
  const auto refuse = [&](const char* why) {
    *reason = why;
    return false;
  };
  if (bytes.size() < VCACHE_TRACE_HEADER_BYTES) return refuse("truncated header");
  vcache_trace_header header;
  std::memcpy(&header, bytes.data(), sizeof(header));
  if (std::memcmp(&header, &vcache_trace_header_v1, 32) != 0 ||
      !ZeroTracePadding(bytes.substr(40, VCACHE_TRACE_HEADER_BYTES - 40))) {
    return refuse("unsupported header layout");
  }
  if (header.incomplete != 0) return refuse("writer reported a lost record");
  if (header.reserved_chunks == 0 || header.reserved_chunks > VCACHE_TRACE_MAX_CHUNKS) {
    return refuse("invalid reservation count");
  }
  const uint64_t expected_bytes = VCACHE_TRACE_HEADER_BYTES +
      static_cast<uint64_t>(header.reserved_chunks) * VCACHE_TRACE_CHUNK_BYTES;
  if (bytes.size() != expected_bytes) return refuse("reservation/file size mismatch");
  for (uint32_t chunk = 0; chunk < header.reserved_chunks; ++chunk) {
    const size_t start = VCACHE_TRACE_HEADER_BYTES + size_t{chunk} * VCACHE_TRACE_CHUNK_BYTES;
    const std::string_view block = bytes.substr(start, VCACHE_TRACE_CHUNK_BYTES);
    uint32_t state;
    std::memcpy(&state, block.data(), sizeof(state));
    if (state != VCACHE_TRACE_CHUNK_COMMITTED ||
        !ZeroTracePadding(block.substr(4, VCACHE_TRACE_CHUNK_HEADER_BYTES - 4))) {
      return refuse("unpublished or unfinished chunk");
    }
    size_t offset = VCACHE_TRACE_CHUNK_HEADER_BYTES;
    bool has_record = false;
    while (offset < block.size()) {
      uint32_t length;
      std::memcpy(&length, block.data() + offset, sizeof(length));
      if (length == 0) {
        if (!ZeroTracePadding(block.substr(offset))) return refuse("unpublished tail");
        break;
      }
      if (length < 4 || length > VCACHE_TRACE_RECORD_MAX ||
          length > block.size() - offset - 4) return refuse("uncommitted or invalid record length");
      const size_t record_bytes = (4 + size_t{length} + 7) & ~size_t{7};
      if (record_bytes > block.size() - offset) return refuse("record crosses chunk boundary");
      const std::string_view record = block.substr(offset + 4, length);
      if (record.back() != '\n' || record.substr(2, length - 3).find_first_of(
              std::string_view("\0\n\t", 3)) != std::string_view::npos ||
          !ZeroTracePadding(block.substr(offset + 4 + length, record_bytes - 4 - length)) ||
          !AppendTraceRecord(record.substr(0, length - 1), out)) return refuse("malformed record");
      has_record = true;
      offset += record_bytes;
    }
    if (!has_record) return refuse("empty reserved chunk");
  }
  return true;
}

#if defined(__linux__) && defined(__LP64__)
bool AllocateTraceHeader(int fd) {
  sigset_t blocked, previous;
  ::sigemptyset(&blocked);
  ::sigaddset(&blocked, SIGXFSZ);
  if (::pthread_sigmask(SIG_BLOCK, &blocked, &previous) != 0) return false;
  int error;
  do { error = ::posix_fallocate(fd, 0, VCACHE_TRACE_HEADER_BYTES); } while (error == EINTR);
  bool complete = false;
  if (error == 0) {
    ssize_t written;
    do { written = ::pwrite(fd, &vcache_trace_header_v1, sizeof(vcache_trace_header_v1), 0); }
    while (written < 0 && errno == EINTR);
    complete = written == sizeof(vcache_trace_header_v1);
    error = written < 0 ? errno : 0;
  }
  if (error == EFBIG && !::sigismember(&previous, SIGXFSZ)) {
    const timespec no_wait{};
    ::sigtimedwait(&blocked, nullptr, &no_wait);
  }
  ::pthread_sigmask(SIG_SETMASK, &previous, nullptr);
  return complete;
}
#endif

}  // namespace

bool PrepareTraceLog(const std::string& log_path) {
#if defined(__linux__) && defined(__LP64__)
  const long page_bytes = ::sysconf(_SC_PAGESIZE);
  if (page_bytes > 0 && VCACHE_TRACE_HEADER_BYTES % page_bytes == 0 &&
      VCACHE_TRACE_CHUNK_BYTES % page_bytes == 0 && __atomic_always_lock_free(4, nullptr)) {
    const int fd = ::open(log_path.c_str(), O_RDWR | O_CREAT | O_EXCL | O_CLOEXEC, 0600);
    if (fd >= 0) {
      struct statfs filesystem {};
      bool mapped = ::fstatfs(fd, &filesystem) == 0 &&
          (filesystem.f_type == 0xef53 || filesystem.f_type == 0x58465342 ||
           filesystem.f_type == 0x9123683e || filesystem.f_type == 0x01021994);
      if (mapped) {
        mapped = AllocateTraceHeader(fd);
      }
      ::close(fd);
      if (mapped) {
        VCACHE_LOG("trace: format=mmap-v1 chunk_bytes=65536 max_chunks=4096");
        return true;
      }
      // This file is ours and no writer has started; text fallback must not
      // inherit a partial binary header or an allocated zero tail.
      util::RemoveFile(log_path);
    }
  }
#endif
  VCACHE_LOG("trace: format=text (mapped setup unavailable)");
  return false;
}

bool IsIgnoredPath(const std::string& path) {
  if (path.empty() || path[0] != '/') return true;  // relative junk from argv

  // Process and kernel state. Never an input; always changing.
  if (HasPrefix(path, "/proc/") || path == "/proc") return true;
  if (HasPrefix(path, "/sys/") || path == "/sys") return true;
  // Character devices used for diagnostics or entropy are not stable file
  // inputs and must never be hashed. Do not exclude all of /dev: /dev/shm is a
  // common build location and can contain real objects and linker scripts.
  if (path == "/dev/null" || path == "/dev/tty" || path == "/dev/random" ||
      path == "/dev/urandom" || HasPrefix(path, "/dev/pts/")) return true;

  // Temporary files are deliberately NOT excluded by prefix. Two reasons, and
  // they point opposite ways:
  //
  //   Excluding /tmp is unsound. TMPDIR is frequently not /tmp -- on the host
  //   this was developed against it is under $HOME -- so a prefix rule misses
  //   the driver's real intermediates, and CI that builds inside /tmp would
  //   have every one of its objects silently dropped from the input set.
  //
  //   Excluding them is also unnecessary. The driver's intermediates are gone
  //   by the time the trace is classified, so they cannot be hashed and are
  //   dropped on that basis in core/link.cc. A temporary that still exists is
  //   indistinguishable from a real input, and is treated as one.

  // Directory self-references the search-path logic produces by the dozen
  // ("/usr/lib/../lib/."). The directory itself is not an input; what was found
  // or not found inside it is, and those are recorded separately. A trailing
  // /.. must stay: clang probes first/sub/.. before trying sub/../a.h, and a
  // missing sub directory is what prevents that include path from winning.
  if (util::EndsWith(path, "/.")) return true;

  return false;
}

bool ReadTraceRecords(const std::string& log_path, TraceRecords* out) {
  struct stat info {};
  if (::stat(log_path.c_str(), &info) != 0 || info.st_size < 0 ||
      static_cast<uint64_t>(info.st_size) > VCACHE_TRACE_MAX_BYTES ||
      !(info.st_mode & S_IRUSR)) return false;
  auto text = util::ReadFile(log_path);
  if (!text) return false;
  TraceRecords records;
  if (text->size() >= 8 && std::memcmp(text->data(), vcache_trace_header_v1.magic, 8) == 0) {
    std::string reason;
    const bool complete = ReadMappedTrace(*text, &records, &reason);
    VCACHE_LOG(complete ? "trace: format=mmap-v1 complete" :
                         "trace: format=mmap-v1 incomplete (" + reason + ")");
    if (!complete) return false;
    *out = std::move(records);
    return true;
  }
  // Every record ends in a newline. A log that does not was cut off mid-write
  // (ENOSPC, RLIMIT_FSIZE) and its last line may be a prefix of a path.
  if (!text->empty() && text->back() != '\n') return false;
  for (const std::string& line : util::Split(*text, '\n', /*skip_empty=*/true)) {
    // The tracer's own word that it lost a record, when it could not create
    // the marker that TraceMarkedIncomplete looks for.
    if (line == "!") return false;
    // A malformed line is a missing dependency record. Partial traces cannot
    // be trusted: skipping one can turn a required input into a false hit.
    if (line.size() < 3 || line[1] != '\t') return false;
    // A tracer from before records were made absolute: joining the record to
    // any directory here can name a file the process never touched.
    if (line[2] != '/') return false;
    if (!AppendTraceRecord(line, &records)) return false;
  }
  *out = std::move(records);
  return true;
}

bool TraceMarkedIncomplete(const std::string& log_path) {
  struct stat st;
  return ::stat((log_path + ".incomplete").c_str(), &st) == 0;
}

std::string TracerPath() {
  const char* override_path = std::getenv("VCACHE_TRACER");
  if (override_path != nullptr && *override_path != '\0') return override_path;
  auto self = util::SelfPath();
  if (!self) return "";
  return util::DirName(*self) + "/vcache-fstrace.so";
}

bool TracerLoadsHere(const std::string& tracer) {
  // e_ident's class and data bytes, e_type and e_machine all sit in the first
  // 20 bytes, at the same offsets in both classes.
  const auto header = [](const std::string& path) -> std::optional<std::string> {
    const int fd = ::open(path.c_str(), O_RDONLY | O_CLOEXEC);
    if (fd < 0) return std::nullopt;
    std::string bytes(20, '\0');
    const ssize_t got = ::read(fd, bytes.data(), bytes.size());
    ::close(fd);
    if (got != static_cast<ssize_t>(bytes.size()) || bytes.compare(0, 4, "\x7f" "ELF") != 0) {
      return std::nullopt;
    }
    return bytes;
  };
  const std::optional<std::string> self = util::SelfPath();
  if (!self) return false;
  const std::optional<std::string> ours = header(*self);
  const std::optional<std::string> theirs = header(tracer);
  if (!ours || !theirs) return false;
  constexpr char kSharedObjectType = 3;  // ET_DYN
  const bool little_endian = (*theirs)[5] == 1;
  const char type = (*theirs)[little_endian ? 16 : 17];
  return (*ours)[4] == (*theirs)[4] && (*ours)[5] == (*theirs)[5] &&
         ours->compare(18, 2, *theirs, 18, 2) == 0 && type == kSharedObjectType;
}

bool TracerPathPreloadable(const std::string& tracer) {
  return tracer.find_first_of(" :") == std::string::npos;
}

std::string WithoutPreloadRefusals(const std::string& stderr_text, const std::string& tracer,
                                   bool* removed) {
  // ld.so writes "ERROR: ld.so: object '<path>' from LD_PRELOAD cannot be
  // preloaded (<why>): ignored.\n" in one write, but another process of the
  // tree can write between the pieces of a diagnostic it is printing, so only
  // that span goes, never the rest of the line it landed in. Older glibc omits
  // " (<why>)".
  static constexpr std::string_view kLead = "ERROR: ld.so: object ";
  static constexpr std::string_view kEnd = ": ignored.";
  const std::string core = "'" + tracer + "' " + std::string(kPreloadIgnoredMessage);
  *removed = false;
  std::string kept;
  kept.reserve(stderr_text.size());
  size_t from = 0;
  while (true) {
    const size_t found = stderr_text.find(core, from);
    if (found == std::string::npos) break;
    size_t begin = found;
    if (begin >= kLead.size() &&
        stderr_text.compare(begin - kLead.size(), kLead.size(), kLead) == 0) {
      begin -= kLead.size();
    }
    size_t after = found + core.size();
    if (stderr_text.compare(after, 2, " (") == 0) {
      // The reason may hold parentheses of its own; it ends at "): ignored.".
      const size_t close = stderr_text.find(")" + std::string(kEnd), after);
      const size_t newline = stderr_text.find('\n', after);
      if (close != std::string::npos && (newline == std::string::npos || close < newline)) {
        after = close + 1;
      }
    }
    if (stderr_text.compare(after, kEnd.size(), kEnd) == 0) {
      after += kEnd.size();
      if (after < stderr_text.size() && stderr_text[after] == '\n') ++after;
    } else {
      // Not the shape ld.so writes: drop through to the end of the line, as
      // nothing after it can be told apart from the refusal.
      const size_t newline = stderr_text.find('\n', after);
      after = newline == std::string::npos ? stderr_text.size() : newline + 1;
    }
    kept.append(stderr_text, from, begin - from);
    *removed = true;
    from = after;
  }
  kept.append(stderr_text, from, std::string::npos);
  return kept;
}

bool ParseTraceLog(const std::string& log_path,
                   const std::vector<std::string>& outputs,
                   const std::string& cwd, LinkTrace* out) {
  TraceRecords records;
  if (!ReadTraceRecords(log_path, &records)) return false;

  std::vector<std::string> abs_outputs;
  abs_outputs.reserve(outputs.size());
  for (const std::string& o : outputs) {
    abs_outputs.push_back(util::AbsoluteLexical(
        o.empty() || o[0] == '/' ? o : cwd + "/" + o));
  }
  const auto is_output = [&](const std::string& p) {
    return std::find(abs_outputs.begin(), abs_outputs.end(), p) !=
           abs_outputs.end();
  };

  // A linker's own implementation is a shared library -- ld.bfd's logic lives
  // in libbfd, lld's in libLLVM -- so what the processes loaded is an input.
  // A file written is kept as one too, as it was before the tracer told the
  // two apart: an intermediate is gone by the time it would be hashed.
  std::vector<std::string> reads, absent, tools;
  const auto keep = [&](const std::vector<std::string>& from, std::vector<std::string>* to) {
    for (const std::string& raw : from) {
      std::string path = util::AbsoluteLexical(raw);
      if (IsIgnoredPath(path) || is_output(path)) continue;
      to->push_back(std::move(path));
    }
  };
  keep(records.reads, &reads);
  keep(records.loaded, &reads);
  keep(records.written, &reads);
  keep(records.absent, &absent);
  keep(records.tools, &tools);

  const auto sort_unique = [](std::vector<std::string>* v) {
    std::sort(v->begin(), v->end());
    v->erase(std::unique(v->begin(), v->end()), v->end());
  };
  sort_unique(&reads);
  sort_unique(&absent);
  sort_unique(&tools);

  // A path that was both probed-absent and later read is an input, not an
  // absence: the search found it on a later attempt, or it was created during
  // the link. Keeping it in both sets would make the entry unhittable.
  std::vector<std::string> only_absent;
  std::set_difference(absent.begin(), absent.end(), reads.begin(), reads.end(),
                      std::back_inserter(only_absent));

  out->inputs = std::move(reads);
  out->absent = std::move(only_absent);
  out->tools = std::move(tools);
  return true;
}

}  // namespace vcache::core
