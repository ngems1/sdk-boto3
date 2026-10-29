# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [1.0.0] - 2026-10-29

### Added
- `ebs-audit scan`: AWS Organizations account discovery (whole org or OU subtree), cross-account `sts:AssumeRole` with ExternalId, per-account enabled-region discovery, and parallel scanning of unattached EBS volumes older than N days.
- Cost estimation for gp2/gp3/io1/io2/st1/sc1/standard, including provisioned IOPS (tiered for io2) and throughput, with JSON price overrides and optional live AWS Price List lookups.
- Reports in CSV, JSON and Markdown (summary, breakdowns by account/region/type, top volumes, errors).
- Deletion plan (`--plan`): reviewable JSON with a checksum, expiry, protect-tag exclusions and snapshot cost estimate.
- `ebs-audit apply-plan`: `--dry-run` permission check, re-verification of each volume, snapshot-before-delete (re-used across retries), confirmation prompt, per-volume results file.
- Least-privilege IAM policies and a CloudFormation StackSet template for the member-account role.
- Unit tests with in-memory fakes, moto end-to-end tests, ruff, strict mypy, GitHub Actions CI and a Dockerfile.
