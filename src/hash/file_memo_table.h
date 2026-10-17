// SPDX-FileCopyrightText: 2026 Unto Labs
// SPDX-License-Identifier: Apache-2.0
#pragma once

#include <cstddef>
#include <cstdint>

namespace vcache::hash::file_memo {

inline constexpr char kTableName[] = "table-v1";
inline constexpr uint32_t kTableVersion = 1;
inline constexpr size_t kHeaderBytes = 4096;
inline constexpr size_t kSlotCount = 16384;
inline constexpr size_t kProbeCount = 32;
inline constexpr uint32_t kEmpty = 0;
inline constexpr uint32_t kWriting = 1;
inline constexpr uint32_t kCommitted = 2;

struct FileIdentity {
  uint64_t device = 0;
  uint64_t inode = 0;
  uint64_t size = 0;
  int64_t mtime_ns = 0;
  int64_t ctime_ns = 0;

  bool operator==(const FileIdentity&) const = default;
};

struct TableHeader {
  char magic[8];
  uint32_t version;
  uint32_t byte_order;
  uint32_t header_bytes;
  uint32_t slot_bytes;
  uint32_t slot_count;
  uint32_t reserved;
};

struct alignas(64) TableSlot {
  uint32_t state;
  uint32_t reserved;
  FileIdentity identity;
  char digest[64];
  char checksum[64];
};

static_assert(sizeof(FileIdentity) == 40);
static_assert(sizeof(TableHeader) == 32);
static_assert(sizeof(TableSlot) == 192);
static_assert(offsetof(TableSlot, digest) == offsetof(TableSlot, identity) + sizeof(FileIdentity));
inline constexpr size_t kTableBytes = kHeaderBytes + sizeof(TableSlot) * kSlotCount;
inline constexpr TableHeader kHeader{
    {'V', 'C', 'M', 'E', 'M', 'O', '\r', '\n'}, kTableVersion, 0x01020304,
    kHeaderBytes, sizeof(TableSlot), kSlotCount, 0};

}  // namespace vcache::hash::file_memo
