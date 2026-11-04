# NewAPI application deployment

Supports the existing single-instance Docker Compose + PostgreSQL + Redis installation. Replaces only the application image. Releases that intentionally change schema, ORM, database drivers/behavior or service topology require a reviewed upgrade plan; do not bypass checks. A short maintenance window is expected.

The default `database_mode=unchanged` rejects every schema change. The reviewed
`--database-mode usage-tags-v1` release mode permits only adding
`logs.usage_tag` and `logs.usage_ref` as nullable `varchar(64) DEFAULT ''`, plus
the non-unique btree `idx_logs_usage_tag (usage_tag)` index. This is an explicit
mode on `build_release.py`, recorded in the immutable release manifest, not a
general migration bypass. Application startup performs the normal GORM
migration; the deploy tool does not run migration SQL. No other column, type,
default, constraint, index or table change is accepted.

For this mode, preparation restores the production backup into the isolated
PostgreSQL clone, starts the new image twice, and checks the exact allowed DDL,
unchanged pre-existing log fields/row counts, empty values in the two new
columns, and the existing full business-table fingerprints. It then stops the
new clone application and starts the currently running old image against that
upgraded clone. Old-image health/version and unchanged full schema/data are
required; cutover rejects missing rollback-rehearsal evidence. Production uses
the same strict schema/data comparison before reopening ingress. If columns
already exist, all log values and the full schema must remain unchanged.

The separate `--database-mode usage-tag-time-index-v1` mode is for an installation
that already has those usage columns and the original single-column index. It
permits only adding the non-unique btree index
`idx_logs_usage_tag_created_at (usage_tag, created_at)` on `public.logs`.
Missing/wrong usage-column shapes, missing/wrong original index, different key
order, unique/partial/covering indexes, and every other schema difference are
rejected. All existing log values (including both usage columns), row counts,
and business-table fingerprints must remain unchanged. This mode cannot combine
the original column migration with the new index migration. If the index already
exists, the full schema must remain unchanged.

Both additive modes require the new image to start twice on the isolated
production-backup clone and the old image to start successfully on that upgraded
clone. Preparation/cutover record the exact schema comparison and full business
data checks; collection requires their snapshots and old-image compatibility
evidence. Neither mode changes the default `unchanged` checks or the other's
allowlist. Select `usage-tag-time-index-v1` in the build workflow's
`database_mode` input for that reviewed index-only release.

Rollback preserves these additive columns/indexes and switches only the app
image/config. Never drop the new columns/index or restore an earlier database to
roll back this release: that would discard later data. Keep the initial/final
database archives, snapshot comparisons, old-image compatibility evidence and
original configuration in the collected recovery bundle. Any failed rehearsal
requires inspecting its evidence and preparing a fresh release directory;
do not relax the allowlist to accommodate an unexpected difference.

## Configuration and source

The target profile is a private local JSON file, normally `~/.config/newapi-deploy/aws-packapi.json` (0600, parent 0700). It contains SSH binding, the `target-ssh-controller/scripts/target-ssh.sh` path, and a `remote` object describing existing Compose services. Existing runtime-base/tool paths can still be used for an isolated local build check; production builds run in GitHub Actions. Credentials are referenced through an SSH identity or password environment variable; an optional literal-assignment file supports the existing local setup without sourcing shell code. Never commit profiles, keys, artifacts or backups.

Use the manually dispatched `.github/workflows/ghcr-release.yml` workflow in `youzhou-ali/new-api`. The workflow must first be committed to the default branch. Select the tested `source_ref` and reviewed `database_mode`; it resolves the source to a commit and uses that source's Go/Bun versions. `build_release.py` uses `git archive`; uncommitted changes are excluded. The real frontend is embedded in the Linux amd64 binary, which is packaged with licenses and the digest-pinned runtime base. An empty `web/dist` from ordinary CI is not a release artifact.

The workflow publishes `ghcr.io/youzhou-ali/new-api:<release-id>` with its repository-scoped `GITHUB_TOKEN` (`packages:write`). Only after publishing returns a SHA256 digest does `--finalize` create format-2 `release.json`, binding the source commit, frontend/binary hashes, database mode and `image_ref`. The deployment image reference is always `ghcr.io/youzhou-ali/new-api@sha256:...`; tags are informational. The only uploaded Actions artifact is `newapi-release-<release-id>/release.json`; source, build logs, binaries and backups are excluded. This workflow does not connect to AWS or deploy on push.

New staging/preparation/cutover accept only format 2; the old binary-upload/server-build path is removed. Historical format-1 releases remain readable by `status` and `collect`, including collection of a previous release's still-remote recovery bundle. Collection requires the local manifest bytes to match the recorded remote manifest; historical artifacts cannot be restaged with the new tooling.

Before the first AWS pull, ensure the GHCR package is readable from the server. New GHCR packages default to private: either explicitly make this image public for anonymous pulls, or provision an existing protected Docker login for an account with package read access and a classic PAT with `read:packages`. Do not give AWS a publishing token or place registry credentials in the release manifest, target JSON, Git or logs. The deployment scripts use the server's existing Docker authentication; they do not create credentials or change package visibility.

Download the manifest from the successful workflow run, using its exact run/artifact identity (shown in the workflow summary):

```sh
umask 077
mkdir -p "$NEWAPI_RELEASE"
gh run download "$NEWAPI_RUN_ID" --repo youzhou-ali/new-api \
  --name "newapi-release-$NEWAPI_RELEASE_ID" --dir "$NEWAPI_RELEASE"
```

Run from the repository root. Populate the example variables from the private profile and downloaded release manifest. Read JSON in Python and pass values as argument lists; do not `eval` generated assignments. `NEWAPI_RELEASE` holds the downloaded `release.json` and later the private recovery files. Keep that directory unchanged when resuming/verifying/collecting. Write commands require `--execute` under existing deployment authorization; the flag is not another approval ritual.

```sh
python3 ops/deploy/deploy.py inspect --target "$NEWAPI_TARGET"
python3 ops/deploy/deploy.py stage --target "$NEWAPI_TARGET" --release "$NEWAPI_RELEASE" --execute
python3 ops/deploy/deploy.py prepare --target "$NEWAPI_TARGET" --release "$NEWAPI_RELEASE" --execute
python3 ops/deploy/deploy.py cutover --target "$NEWAPI_TARGET" --release "$NEWAPI_RELEASE" --execute
python3 ops/deploy/deploy.py verify --target "$NEWAPI_TARGET" --release "$NEWAPI_RELEASE"
python3 ops/deploy/deploy.py collect --target "$NEWAPI_TARGET" --release "$NEWAPI_RELEASE" --execute
```

## Stages

- `inspect`: selected container/mount/version information, excluding environment and credentials.
- `stage`: validates the digest-bound manifest and uploads only immutable metadata/helpers. AWS pulls the exact GHCR digest before any maintenance and checks its registry digest, Linux amd64 platform, source/version labels and actual `/new-api` SHA256 in a temporary container with no network, production mounts or extra capabilities. Pull output remains in private `image-pull.log`. Repeating `stage` requires identical metadata and reuses Docker's cached layers; no app replacement.
- `prepare`: holds the target lock, records current image/config and rollback image, revalidates the already-pulled image, validates an initial DB backup, restores to isolated PostgreSQL/Redis on an internal network, and verifies startup twice. It does not build or pull images. Resources belong only to that rehearsal and are cleaned afterward.
- `cutover`: holds the same target-wide lock, binds candidate/rollback IDs to preparation evidence, closes NewAPI ingress and the direct port, drains requests/batch updates, stops only the app, takes final database/data/log backups, updates the app, checks health/mounts/containers/data, then reopens ingress. No DB/Redis recreation, `down -v`, volume pruning or DB restoration.
- `verify`: deployed image and successful cutover, then public version and frontend entry asset hashes. An explicitly designated `--api-key-file /private/path` also checks `/v1/models`. No paid provider calls.
- `collect`: checksummed off-server recovery archive with final DB, roles, config, application data and audit metadata. Downloads in 4 MiB requests; `recovery-data.partial` and its private `.partial.json` identity checkpoint survive interruption. On retry, it verifies remote path/size/SHA256 and the existing local byte prefix, then appends from that exact offset, including a partial final chunk. Each completed request is flushed/synced; the full SHA256 gates the final rename and `recovery.json`. Existing completed archives are verified and reused. Logs stay in remote `application-final.tgz`. The archive contains secrets and remains private; downloading is not restoration.

Only explicit system-task/instance metadata and Casbin ID rewrites with identical policy content/count are allowed startup mutations. Rehearsal and cutover use the same checks. Schema or unexpected business-data changes stop the release.

## Uncertain transport, failure and rollback

An interrupted `stage` can be retried with the identical manifest and helpers; pull/verification precedes preparation and ingress changes. Docker reuses completed image layers. An interrupted `collect` can be rerun with the same release directory; do not delete its partial file/checkpoint. A changed remote archive identity, corrupt local prefix, missing checkpoint or checksum mismatch stops collection for investigation. Fix the cause and explicitly set aside unusable partial/checkpoint files before starting over; never present them as a completed backup.

SSH timeout does not establish whether preparation/cutover completed. Inspect first:

```sh
python3 ops/deploy/deploy.py status --target "$NEWAPI_TARGET" --release "$NEWAPI_RELEASE"
# Without local artifacts:
python3 ops/deploy/deploy.py status --target "$NEWAPI_TARGET" --release-id "$NEWAPI_RELEASE_ID"
```

Read remote `prepare-state.json` and backup `cutover-state.json`; detailed evidence stays private. Preparation/cutover cannot be blindly rerun after state exists. Check the live process and `ROOT/.newapi-deploy.lock`; never delete a lock file to bypass a running operation. `stage` can retry because metadata is published atomically and verified before the digest-pinned pull. Checksum disagreement needs investigation, not overwriting.

A caught cutover failure restores the saved old app image/config. Ingress reopens only after old-app health passes. The database is never automatically restored. Failed old-app startup retains maintenance and `rollback_incomplete` state.

After SIGKILL/host failure or incomplete rollback, first inspect state, live image/health, Compose/Nginx hashes and recorded firewall rules. Determine completed steps and preserve all backups/current data. Under recovery authorization, restore the saved application image/config, verify compatibility with the current DB and app health, restore original Nginx **in place** (bind-mounted file), run `nginx -t` and reload, then remove only the exact recorded rules. Never flush firewall chains or stop shared Nginx. Do not execute historical cutover scripts.

Rollback after a successful release is a **new** application release of a known prior artifact with fresh backup/state and checked DB compatibility. Old state is evidence, not a replay command. Rebuilt old artifacts retain the old source commit and get fresh validation.

Database restore requires its own explicit recovery point/authorization: first back up current data, rehearse restoration in isolation, and account for writes since the selected backup. Never automatically overwrite newer users, balances, keys or records with a pre-deploy dump.

If public verification fails after cutover, inspect version, ingress, cache and image before another mutation. If only backup download fails, keep the healthy app running and retry collection; report the off-server copy incomplete until its checksum is verified. Container health alone is not full deployment completion.

## Local verification

```sh
python3 -m unittest discover -s ops/deploy -p 'test_*.py' -v
```

Tests use isolated fixtures, never AWS or paid providers. A full local build verifies artifact generation. The refactored automation's first real release still needs its normal rehearsal, cutover and public verification; local test success is not production deployment evidence.
