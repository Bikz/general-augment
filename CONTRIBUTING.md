# Contributing

Thanks for your interest in General Augment.

## This repository is a read-only mirror

The public `general-augment` repository (SDKs, CLI, and examples) is an
**auto-generated, read-only mirror** of a private monorepo where development
actually happens. The published packages are built from that upstream source:

- `general-augment-cli` (PyPI)
- `general-augment-sdk` (PyPI)
- `@general-augment/sdk` (npm)

Because the mirror is regenerated from upstream, any commit pushed directly to it
would be overwritten on the next sync. **We therefore cannot accept code pull
requests on the mirror** — they have nowhere to land.

## What we welcome

- **Bug reports** — open an issue. Please include the package and version
  (`genaug --version`, the SDK `VERSION` / `__version__`), a minimal
  reproduction, and what you expected.
- **Documentation problems** — stale examples, broken snippets, unclear wording.
  File an issue and we will fix it upstream and re-mirror.
- **Feature requests and feedback** — open an issue describing the use case.

## What we cannot accept here

- **Code pull requests.** The mirror's source of truth is upstream and private,
  so PRs against generated files (including the SDK code under
  `packages/*/src/_generated`) cannot be merged. If you have a fix in mind,
  describe it in an issue and we will implement it upstream with attribution.

## Security issues

Please do not file security problems as public issues. Email
`security@generalaugment.com` instead.
