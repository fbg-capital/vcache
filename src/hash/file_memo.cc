// SPDX-FileCopyrightText: 2026 Unto Labs
// SPDX-License-Identifier: Apache-2.0
#include "hash/file_memo.h"

#include <sys/stat.h>
#if defined(__linux__)
#include <fcntl.h>
#include <sys/mman.h>
#include <sys/vfs.h>
#include <unistd.h>
#endif

#include <algorithm>
#include <cerrno>
#include <cstring>
#include <ctime>
#include <memory>

#include "hash/file_memo_table.h"
#include "hash/hasher.h"
#include "util/fs.h"

namespace vcache::hash {
namespace {

// A writer can still change a file within the granularity of its timestamps
// without moving them, so a file touched this recently is hashed but not
// memoised; a later lookup memoises it once it has settled.
constexpr int64_t kUnsettledWindowNs = 2'000'000'000;

int64_t ToNs(const struct timespec& ts) {
  return static_cast<int64_t>(ts.tv_sec) * 1'000'000'000 + static_cast<int64_t>(ts.tv_nsec);
}

using file_memo::FileIdentity;

std::optional<FileIdentity> StatIdentity(const std::string& path) {
  struct stat st;
  if (::stat(path.c_str(), &st) != 0) return std::nullopt;
  FileIdentity id;
  id.device = static_cast<uint64_t>(st.st_dev);
  id.inode = static_cast<uint64_t>(st.st_ino);
  id.size = static_cast<uint64_t>(st.st_size);
#if defined(__APPLE__)
  id.mtime_ns = ToNs(st.st_mtimespec);
  id.ctime_ns = ToNs(st.st_ctimespec);
#else
  id.mtime_ns = ToNs(st.st_mtim);
  id.ctime_ns = ToNs(st.st_ctim);
#endif
  return id;
}

std::string MemoKey(const FileIdentity& id) {
  Hasher key;
  key.UpdateDelimited("file-digest-memo-v1");
  key.UpdateU64(id.device);
  key.UpdateU64(id.inode);
  key.UpdateU64(id.size);
  key.UpdateU64(static_cast<uint64_t>(id.mtime_ns));
  key.UpdateU64(static_cast<uint64_t>(id.ctime_ns));
  return key.Hex();
}

bool IsDigest(std::string_view digest) {
  return digest.size() == kDigestHexLen &&
         std::all_of(digest.begin(), digest.end(), [](char value) {
           return (value >= '0' && value <= '9') || (value >= 'a' && value <= 'f');
         });
}

class DigestTable {
 public:
  explicit DigestTable(const std::string& memo_dir) {
#if defined(__linux__)
    using namespace file_memo;
    if (!__atomic_always_lock_free(sizeof(uint32_t), nullptr)) return;
    if (!util::MakeDirs(memo_dir)) return;
    const std::string path = memo_dir + "/" + kTableName;
    bool created = false;
    int fd = ::open(path.c_str(), O_RDWR | O_CLOEXEC | O_NOFOLLOW | O_CREAT | O_EXCL, 0600);
    if (fd >= 0) {
      created = true;
    } else if (errno == EEXIST) {
      fd = ::open(path.c_str(), O_RDWR | O_CLOEXEC | O_NOFOLLOW);
    }
    if (fd < 0) return;
    struct stat stat_buffer {};
    struct statfs filesystem {};
    // Shared-memory atomics are only used on local filesystems with coherent mappings.
    bool supported = ::fstatfs(fd, &filesystem) == 0 &&
        (filesystem.f_type == 0xef53 || filesystem.f_type == 0x58465342 ||
         filesystem.f_type == 0x9123683e || filesystem.f_type == 0x01021994);
    if (created && supported) {
      int allocation_error;
      do {
        allocation_error = ::posix_fallocate(fd, 0, kTableBytes);
      } while (allocation_error == EINTR);
      if (allocation_error != 0 ||
          ::pwrite(fd, &kHeader, sizeof(kHeader), 0) != sizeof(kHeader)) supported = false;
    }
    TableHeader header {};
    if (supported && ::fstat(fd, &stat_buffer) == 0 && S_ISREG(stat_buffer.st_mode) &&
        stat_buffer.st_size == kTableBytes &&
        ::pread(fd, &header, sizeof(header), 0) == sizeof(header) &&
        std::memcmp(&header, &kHeader, sizeof(header)) == 0) {
      void* mapping = ::mmap(nullptr, kTableBytes, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
      if (mapping != MAP_FAILED) mapping_ = static_cast<char*>(mapping);
    }
    ::close(fd);
#endif
  }

  ~DigestTable() {
#if defined(__linux__)
    if (mapping_) ::munmap(mapping_, file_memo::kTableBytes);
#endif
  }

  bool Available() const {
    return mapping_ && std::memcmp(mapping_, &file_memo::kHeader,
                                   sizeof(file_memo::kHeader)) == 0;
  }

  std::optional<std::string> Lookup(const FileIdentity& identity, const std::string& key) {
#if defined(__linux__)
    using namespace file_memo;
    if (!Available()) return std::nullopt;
    for (size_t probe = 0; probe < kProbeCount; ++probe) {
      const TableSlot& slot = Slot(key, probe);
      if (__atomic_load_n(&slot.state, __ATOMIC_ACQUIRE) != kCommitted) continue;
      if (slot.identity != identity) continue;
      const std::string digest(slot.digest, kDigestHexLen);
      if (IsDigest(digest) && Checksum(slot) == std::string_view(slot.checksum, kDigestHexLen)) {
        return digest;
      }
    }
#endif
    return std::nullopt;
  }

  bool Store(const FileIdentity& identity, const std::string& key, const std::string& digest) {
#if defined(__linux__)
    using namespace file_memo;
    if (!Available()) return false;
    for (size_t probe = 0; probe < kProbeCount; ++probe) {
      TableSlot& slot = Slot(key, probe);
      uint32_t expected = kEmpty;
      if (!__atomic_compare_exchange_n(&slot.state, &expected, kWriting, false,
                                       __ATOMIC_ACQ_REL, __ATOMIC_ACQUIRE)) continue;
      slot.identity = identity;
      std::memcpy(slot.digest, digest.data(), kDigestHexLen);
      const std::string checksum = Checksum(slot);
      std::memcpy(slot.checksum, checksum.data(), kDigestHexLen);
      // Slots are never reused: readers cannot race a payload rewrite or an ABA cycle.
      __atomic_store_n(&slot.state, kCommitted, __ATOMIC_RELEASE);
      return true;
    }
#endif
    return false;
  }

 private:
  static std::string Checksum(const file_memo::TableSlot& slot) {
    Hasher checksum;
    checksum.Update(&slot.identity, sizeof(slot.identity));
    checksum.Update(slot.digest, sizeof(slot.digest));
    return checksum.Hex();
  }

  file_memo::TableSlot& Slot(const std::string& key, size_t probe) {
    uint32_t bucket = 0;
    for (size_t index = 0; index < 8; ++index) {
      const char digit = key[index];
      bucket = (bucket << 4) | static_cast<uint32_t>(digit <= '9' ? digit - '0' : digit - 'a' + 10);
    }
    auto* slots = reinterpret_cast<file_memo::TableSlot*>(mapping_ + file_memo::kHeaderBytes);
    return slots[(bucket + probe) % file_memo::kSlotCount];
  }

  char* mapping_ = nullptr;
};

DigestTable& TableFor(const std::string& memo_dir) {
  // One mapping per thread bounds address space even when a process visits many stores.
  thread_local std::string current_dir;
  thread_local std::unique_ptr<DigestTable> table;
  const std::string directory = !memo_dir.empty() && memo_dir.front() == '/'
      ? memo_dir : util::CurrentDir() + "/" + memo_dir;
  if (!table || current_dir != directory) {
    table = std::make_unique<DigestTable>(directory);
    current_dir = directory;
  }
  return *table;
}

}  // namespace

std::string FileDigestMemoDir(const std::string& cache_dir) { return cache_dir + "/filehash"; }

std::optional<std::string> HashFileMemoized(const std::string& path,
                                            const std::string& memo_dir) {
  return HashFileMemoizedAt(path, memo_dir, util::NowUnixNs());
}

std::optional<std::string> HashFileMemoizedAt(const std::string& path,
                                              const std::string& memo_dir,
                                              int64_t now_unix_ns) {
  const std::optional<FileIdentity> before = StatIdentity(path);
  if (!before) return HashFile(path);

  const bool settled = now_unix_ns - before->mtime_ns >= kUnsettledWindowNs &&
                       now_unix_ns - before->ctime_ns >= kUnsettledWindowNs;
  if (!settled) return HashFile(path);

  const std::string key = MemoKey(*before);
  DigestTable& table = TableFor(memo_dir);
  if (auto cached = table.Lookup(*before, key)) return cached;
  const std::string memo_path = memo_dir + "/" + key;
  if (auto cached = util::ReadFile(memo_path)) {
    // Written without fsync, so a crash can leave it empty or, on some
    // filesystems, full of NULs; anything but a whole hex digest is a miss.
    if (IsDigest(*cached)) {
      table.Store(*before, key, *cached);
      return *cached;
    }
  }

  auto digest = HashFile(path);
  if (!digest) return std::nullopt;

  // A path renamed onto, or a file rewritten, while it was being hashed would
  // otherwise store the new contents' digest under the old identity.
  if (StatIdentity(path) == before && !table.Store(*before, key, *digest)) {
    // Not durable: losing a memo costs one re-hash, while an fsync per header
    // would land on the first lookup after every checkout.
    util::WriteFileAtomic(memo_path, *digest, /*durable=*/false);
  }
  return digest;
}

size_t PruneFileDigestMemos(const std::string& cache_dir, int64_t max_age_seconds) {
  const std::string dir = FileDigestMemoDir(cache_dir);
  if (!util::IsDirectory(dir)) return 0;
  const int64_t now = static_cast<int64_t>(std::time(nullptr));
  size_t removed = 0;
  for (const util::FileEntry& entry : util::ListFilesRecursive(dir)) {
    if (now - entry.lru_time > max_age_seconds && util::RemoveFile(entry.path)) ++removed;
  }
  return removed;
}

}  // namespace vcache::hash
