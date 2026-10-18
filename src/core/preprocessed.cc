// SPDX-FileCopyrightText: 2026 Unto Labs
// SPDX-License-Identifier: Apache-2.0
#include "core/preprocessed.h"

#include <cctype>
#include <cstdio>
#include <cstring>
#include <vector>

namespace vcache::core {
namespace {

// Un-escapes a C string body as the preprocessor writes it. On Linux this is
// almost always a no-op, but a path containing a backslash or quote would
// otherwise round-trip incorrectly.
std::string Unescape(const std::string& s) {
  std::string out;
  out.reserve(s.size());
  for (size_t i = 0; i < s.size(); ++i) {
    if (s[i] == '\\' && i + 1 < s.size()) {
      out.push_back(s[++i]);
    } else {
      out.push_back(s[i]);
    }
  }
  return out;
}

std::string Escape(const std::string& s) {
  std::string out;
  out.reserve(s.size());
  for (char c : s) {
    if (c == '\\' || c == '"') out.push_back('\\');
    out.push_back(c);
  }
  return out;
}

}  // namespace

bool ContainsIncbin(std::string_view line) {
  // A plain substring search. The directive may be spelled ".incbin" inside a
  // string literal in an asm() statement, or bare in preprocessed assembler,
  // and may be preceded by a tab, a quote or an escape -- so anchoring on
  // anything more than the directive itself only adds ways to miss it. Erring
  // towards "found" costs a cache entry; erring the other way costs a wrong
  // object, which is not a trade worth making.
  static constexpr std::string_view kDirective = ".incbin";
  return line.find(kDirective) != std::string_view::npos;
}

std::string NormalizeLinemarker(const std::string& line, const RootMap& roots) {
  // Shape: `# <number> "<path>"[ flags...]`. Anything else passes through.
  if (line.empty() || line[0] != '#') return line;

  size_t i = 1;
  while (i < line.size() && (line[i] == ' ' || line[i] == '\t')) ++i;
  if (i >= line.size() || std::isdigit(static_cast<unsigned char>(line[i])) == 0) {
    return line;
  }
  while (i < line.size() && std::isdigit(static_cast<unsigned char>(line[i]))) ++i;
  while (i < line.size() && (line[i] == ' ' || line[i] == '\t')) ++i;
  if (i >= line.size() || line[i] != '"') return line;

  const size_t open_quote = i;
  size_t j = open_quote + 1;
  while (j < line.size()) {
    if (line[j] == '\\') {
      j += 2;
      continue;
    }
    if (line[j] == '"') break;
    ++j;
  }
  if (j >= line.size()) return line;  // unterminated; leave alone

  const std::string raw = line.substr(open_quote + 1, j - open_quote - 1);
  const std::string mapped = roots.Canonicalize(Unescape(raw));

  std::string out;
  out.reserve(line.size() + mapped.size());
  out.append(line, 0, open_quote + 1);
  out.append(Escape(mapped));
  out.append(line, j, std::string::npos);
  return out;
}

struct NormalizedPreprocessHash::Impl {
  const RootMap* roots = nullptr;
  hash::Hasher* hasher = nullptr;
  bool* saw_incbin = nullptr;
  hash::Hasher* also = nullptr;
  std::vector<std::string>* pch_paths = nullptr;
  std::string pending;
  std::string hash_batch;
  bool ok = true;

  void Update(std::string_view text) {
    hasher->Update(text);
    if (also != nullptr) also->Update(text);
  }

  // BLAKE3 hashes several chunks at once with SIMD only when one update spans
  // them; fed a run or a linemarker at a time (about 1 KiB apart in real
  // output) it compresses block by block at a fraction of the speed. The
  // digest of a stream does not depend on how it is sliced, so the normalised
  // text is gathered here and handed over in large pieces.
  void Emit(std::string_view text) {
    constexpr size_t kHashBatchBytes = 256 << 10;
    if (hash_batch.size() + text.size() > kHashBatchBytes) {
      Update(hash_batch);
      hash_batch.clear();
      if (text.size() > kHashBatchBytes) {
        Update(text);
        return;
      }
    }
    hash_batch.append(text);
  }

  // `text` is one or more whole lines, none of which starts with '#'.
  void FeedPlain(std::string_view text) {
    Emit(text);
    // Only non-linemarker lines can carry it, and once one has, there is
    // nothing left to learn, so the search stops for the rest of the file.
    // ".incbin" holds no newline, so searching a run of lines finds exactly
    // what searching them one by one would.
    if (saw_incbin != nullptr && !*saw_incbin && ContainsIncbin(text)) {
      *saw_incbin = true;
    }
  }

  void FeedLine(std::string_view line, bool with_newline) {
    constexpr std::string_view kPchPragma = "#pragma GCC pch_preprocess \"";
    if (pch_paths != nullptr && line.starts_with(kPchPragma)) {
      if (line.size() <= kPchPragma.size() || line.back() != '"') {
        ok = false;
        return;
      }
      // gcc prints the name with %s, unescaped: a backslash in it is the
      // path's own, and unescaping would name another file.
      const std::string pch(
          line.substr(kPchPragma.size(), line.size() - kPchPragma.size() - 1));
      pch_paths->push_back(pch);
      Emit(kPchPragma);
      Emit(Escape(roots->Canonicalize(pch)));
      Emit("\"");
    } else if (!line.empty() && line[0] == '#') {
      Emit(NormalizeLinemarker(std::string(line), *roots));
    } else {
      FeedPlain(line);
    }
    if (with_newline) Emit("\n");
  }

  void Push(const char* data, size_t n) {
    if (n == 0) return;
    size_t line_start = 0;
    if (!pending.empty()) {
      const auto* newline = static_cast<const char*>(std::memchr(data, '\n', n));
      if (newline == nullptr) {
        pending.append(data, n);
        return;
      }
      pending.append(data, newline - data);
      FeedLine(pending, true);
      pending.clear();
      line_start = static_cast<size_t>(newline - data) + 1;
    }

    // Everything up to the last newline is whole lines; the rest waits for
    // the next read. Within the whole lines only a '#' that starts a line
    // needs attention, and '#' is rare outside linemarkers, so searching for
    // it skips the per-line work for everything else.
    const size_t last_newline = std::string_view(data, n).rfind('\n');
    const size_t whole_lines_end =
        last_newline == std::string_view::npos || last_newline < line_start ? line_start
                                                                            : last_newline + 1;
    size_t plain_run_start = line_start;
    size_t scan = line_start;
    while (scan < whole_lines_end) {
      const auto* hash_sign =
          static_cast<const char*>(std::memchr(data + scan, '#', whole_lines_end - scan));
      if (hash_sign == nullptr) break;
      const size_t hash_line_start = static_cast<size_t>(hash_sign - data);
      scan = hash_line_start + 1;
      if (hash_line_start != plain_run_start && data[hash_line_start - 1] != '\n') continue;
      if (hash_line_start > plain_run_start) {
        FeedPlain(std::string_view(data + plain_run_start, hash_line_start - plain_run_start));
      }
      const auto* newline = static_cast<const char*>(
          std::memchr(data + hash_line_start, '\n', whole_lines_end - hash_line_start));
      const size_t hash_line_end = static_cast<size_t>(newline - data);
      FeedLine(std::string_view(data + hash_line_start, hash_line_end - hash_line_start), true);
      plain_run_start = scan = hash_line_end + 1;
    }
    if (whole_lines_end > plain_run_start) {
      FeedPlain(std::string_view(data + plain_run_start, whole_lines_end - plain_run_start));
    }
    if (whole_lines_end < n) pending.assign(data + whole_lines_end, n - whole_lines_end);
  }

  bool Finish() {
    if (ok && !pending.empty()) FeedLine(pending, false);
    if (!hash_batch.empty()) Update(hash_batch);
    hash_batch.clear();
    pending.clear();
    return ok;
  }
};

NormalizedPreprocessHash::NormalizedPreprocessHash(const RootMap& roots, hash::Hasher* hasher,
                                                   bool* saw_incbin, hash::Hasher* also,
                                                   std::vector<std::string>* pch_paths)
    : impl_(new Impl) {
  if (saw_incbin != nullptr) *saw_incbin = false;
  impl_->roots = &roots;
  impl_->hasher = hasher;
  impl_->saw_incbin = saw_incbin;
  impl_->also = also;
  impl_->pch_paths = pch_paths;
  impl_->hash_batch.reserve(256 << 10);
}

NormalizedPreprocessHash::~NormalizedPreprocessHash() { delete impl_; }

void NormalizedPreprocessHash::Push(const char* data, size_t n) { impl_->Push(data, n); }

void NormalizedPreprocessHash::NoteReadError() { impl_->ok = false; }

bool NormalizedPreprocessHash::Finish() { return impl_->Finish(); }

bool HashNormalizedPreprocessedOutput(const std::string& path,
                                      const RootMap& roots,
                                      hash::Hasher* hasher,
                                      bool* saw_incbin, hash::Hasher* also,
                                      std::vector<std::string>* pch_paths) {
  if (saw_incbin != nullptr) *saw_incbin = false;
  FILE* f = ::fopen(path.c_str(), "rb");
  if (f == nullptr) return false;
  NormalizedPreprocessHash stream(roots, hasher, saw_incbin, also, pch_paths);
  std::vector<char> buf(1 << 20);
  while (true) {
    const size_t n = ::fread(buf.data(), 1, buf.size(), f);
    if (n == 0) {
      if (::ferror(f) != 0) stream.NoteReadError();
      break;
    }
    stream.Push(buf.data(), n);
  }
  const bool ok = stream.Finish();
  ::fclose(f);
  return ok;
}

}  // namespace vcache::core
