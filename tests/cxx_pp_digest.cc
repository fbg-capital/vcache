// SPDX-FileCopyrightText: 2026 Unto Labs
// SPDX-License-Identifier: Apache-2.0
// Hashes preprocessor output the way the cache key hashes it.
//
//   cxx_pp_digest --roots SPEC[,SPEC...] FILE
//   cxx_pp_digest --roots SPEC --stdin
//
// SPEC is a RootMap spec ("PATH" or "PATH=TARGET"). Prints the hex digest.
#include <cstdio>
#include <iostream>
#include <string>
#include <vector>

#include "core/preprocessed.h"
#include "core/roots.h"
#include "hash/hasher.h"
#include "util/str.h"

namespace {

std::vector<std::string> SplitSpecs(const std::string& text) {
  return vcache::util::Split(text, ',', /*skip_empty=*/true);
}

}  // namespace

int main(int argc, char** argv) {
  std::string roots_text;
  std::string path;
  bool from_stdin = false;
  for (int i = 1; i < argc; ++i) {
    const std::string arg = argv[i];
    if (arg == "--roots" && i + 1 < argc) {
      roots_text = argv[++i];
    } else if (arg == "--stdin") {
      from_stdin = true;
    } else if (arg == "--help") {
      std::fputs("usage: cxx_pp_digest --roots SPEC[,SPEC...] FILE|--stdin\n", stdout);
      return 0;
    } else if (path.empty() && !from_stdin) {
      path = arg;
    } else {
      std::fprintf(stderr, "cxx_pp_digest: unexpected argument %s\n", arg.c_str());
      return 2;
    }
  }
  if (roots_text.empty() || (path.empty() && !from_stdin)) {
    std::fputs("usage: cxx_pp_digest --roots SPEC[,SPEC...] FILE|--stdin\n", stderr);
    return 2;
  }
  std::vector<std::string> errors;
  const vcache::core::RootMap roots =
      vcache::core::RootMap::FromSpecs(SplitSpecs(roots_text), &errors);
  for (const std::string& error : errors) {
    std::fprintf(stderr, "cxx_pp_digest: %s\n", error.c_str());
  }
  vcache::hash::Hasher hasher;
  bool saw_incbin = false;
  vcache::core::NormalizedPreprocessHash stream(roots, &hasher, &saw_incbin);
  if (from_stdin) {
    char buf[1 << 16];
    while (true) {
      const size_t n = std::fread(buf, 1, sizeof(buf), stdin);
      if (n == 0) {
        if (std::ferror(stdin) != 0) stream.NoteReadError();
        break;
      }
      stream.Push(buf, n);
    }
  } else {
    FILE* file = std::fopen(path.c_str(), "rb");
    if (file == nullptr) {
      std::fprintf(stderr, "cxx_pp_digest: cannot read %s\n", path.c_str());
      return 1;
    }
    char buf[1 << 16];
    while (true) {
      const size_t n = std::fread(buf, 1, sizeof(buf), file);
      if (n == 0) {
        if (std::ferror(file) != 0) stream.NoteReadError();
        break;
      }
      stream.Push(buf, n);
    }
    std::fclose(file);
  }
  if (!stream.Finish()) return 1;
  std::fputs(hasher.Hex().c_str(), stdout);
  std::fputc('\n', stdout);
  if (saw_incbin) std::fputs("incbin\n", stderr);
  return 0;
}
