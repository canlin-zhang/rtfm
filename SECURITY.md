# Security Policy

## Reporting a vulnerability

Please report security issues **privately** — do not open a public issue. Either:

- use GitHub's private vulnerability reporting (the repository's **Security** tab →
  **Report a vulnerability**), or
- email the maintainer at **intellitortoise@gmail.com**.

We aim to acknowledge reports within a few days.

## Threat model / scope

rtfm runs locally and indexes documentation you point it at. By design it:

- reads files **in place** from the Sources you configure and never relocates or exfiltrates
  them; extracted text lives only in a local SQLite index under `~/.rtfm/`;
- does **not** execute document contents, run JavaScript, or OCR images;
- modifies files only on a Source you explicitly mark `mutable` in your manifest, and only
  when you explicitly invoke a mutation tool;
- guards reads against path traversal (a `read` cannot escape its Source directory);
- for `repo` Sources, clones/pulls only the git URLs you configure, using your existing git
  credentials.

### In scope

- Path-traversal or sandbox escape in `read`/indexing.
- Query handling that could crash the server or corrupt the index.
- The launcher or manifest parsing mishandling untrusted input in a harmful way.

### Out of scope

- Vulnerabilities in the documents, repositories, or tools you choose to index.
- The behavior of the AI agent/host that calls rtfm.
- Missing OCR / JavaScript rendering — these are documented limitations, not security issues.

## Supported versions

rtfm is pre-1.0; only the latest `main` is supported.
