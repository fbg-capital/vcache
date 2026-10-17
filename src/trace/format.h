/* SPDX-FileCopyrightText: 2026 Unto Labs
 * SPDX-License-Identifier: Apache-2.0 */
#pragma once

#include <stdint.h>

#define VCACHE_TRACE_VERSION 1U
#define VCACHE_TRACE_BYTE_ORDER 0x01020304U
#define VCACHE_TRACE_HEADER_BYTES 4096U
#define VCACHE_TRACE_CHUNK_BYTES 65536U
#define VCACHE_TRACE_MAX_CHUNKS 4096U
#define VCACHE_TRACE_CHUNK_HEADER_BYTES 64U
#define VCACHE_TRACE_RECORD_MAX 4096U
#define VCACHE_TRACE_RECORD_WRITING 0x80000000U
#define VCACHE_TRACE_CHUNK_WRITING 1U
#define VCACHE_TRACE_CHUNK_COMMITTED 2U
#define VCACHE_TRACE_MAX_BYTES \
  (VCACHE_TRACE_HEADER_BYTES + (uint64_t)VCACHE_TRACE_CHUNK_BYTES * VCACHE_TRACE_MAX_CHUNKS)

struct vcache_trace_header {
  char magic[8];
  uint32_t version;
  uint32_t byte_order;
  uint32_t header_bytes;
  uint32_t chunk_bytes;
  uint32_t max_chunks;
  uint32_t reserved;
  uint32_t reserved_chunks;
  uint32_t incomplete;
  uint32_t padding[6];
};

static const struct vcache_trace_header vcache_trace_header_v1 = {
    {'V', 'C', 'T', 'R', 'A', 'C', 'E', '\0'}, VCACHE_TRACE_VERSION,
    VCACHE_TRACE_BYTE_ORDER, VCACHE_TRACE_HEADER_BYTES, VCACHE_TRACE_CHUNK_BYTES,
    VCACHE_TRACE_MAX_CHUNKS, 0, 0, 0, {0, 0, 0, 0, 0, 0}};

#ifdef __cplusplus
static_assert(sizeof(vcache_trace_header) == 64);
#else
_Static_assert(sizeof(struct vcache_trace_header) == 64, "trace header layout");
#endif
