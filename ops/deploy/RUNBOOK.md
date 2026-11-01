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
allowlist. Specify the mode when building the release, for example append
`--database-mode usage-tag-time-index-v1` to the `build_release.py` command below.

Rollback preserves these additive columns/indexes and switches only the app
image/config. Never drop the new columns/index or restore an earlier database to
roll back this release: that would discard later data. Keep the initial/final
database archives, snapshot comparisons, old-image compatibility evidence and
original configuration in the collected recovery bundle. Any failed rehearsal
requires inspecting its evidence and preparing a fresh release directory;
do not relax the allowlist to accommodate an unexpected difference.

## Configuration and source

The target profile is a private local JSON file, normally `~/.config/newapi-deploy/aws-packapi.json` (0600, parent 0700). It contains SSH binding, the `target-ssh-controller/scripts/target-ssh.sh` path, a digest-pinned runtime base, tool paths, and a `remote` object describing existing Compose services. Credentials are referenced through an SSH identity or password environment variable; an optional literal-assignment file supports the existing local setup without sourcing shell code. Never commit profiles, keys, artifacts or backups.

Use an explicit committed ref. `build_release.py` uses `git archive`; uncommitted changes are excluded. Perform applicable project checks first and retain the tested commit. The build creates actual frontend assets embedded in the Linux amd64 binary; CI using an empty `web/dist` does not validate that artifact. Reuse a validated artifact only when source, tools, runtime base and checksums match.

Run from the repository root. Populate the example variables from the private profile and release manifest. Read JSON in Python and pass values as argument lists; do not `eval` generated assignments. `NEWAPI_RELEASE` is a new directory for a build, or the existing unchanged directory when resuming/verifying/collecting. Write commands require `--execute` under existing deployment authorization; the flag is not another approval ritual.

```sh
python3 ops/deploy/deploy.py inspect --target "$NEWAPI_TARGET"
python3 ops/deploy/build_release.py --repo "$NEWAPI_REPO" --ref "$NEWAPI_COMMIT" \
  --out "$NEWAPI_RELEASE" --runtime-base "$NEWAPI_RUNTIME_BASE" \
  --go "$NEWAPI_GO" --bun "$NEWAPI_BUN"
python3 ops/deploy/deploy.py stage --target "$NEWAPI_TARGET" --release "$NEWAPI_RELEASE" --execute
python3 ops/deploy/deploy.py prepare --target "$NEWAPI_TARGET" --release "$NEWAPI_RELEASE" --execute
python3 ops/deploy/deploy.py cutover --target "$NEWAPI_TARGET" --release "$NEWAPI_RELEASE" --execute
python3 ops/deploy/deploy.py verify --target "$NEWAPI_TARGET" --release "$NEWAPI_RELEASE"
python3 ops/deploy/deploy.py collect --target "$NEWAPI_TARGET" --release "$NEWAPI_RELEASE" --execute
```

## Stages

- `inspect`: selected container/mount/version information, excluding environment and credentials.
- `stage`: verifies the package, uploads immutable metadata/helper hashes, resumes missing 4 MiB chunks, and validates the full artifact. Existing bytes must match; no app replacement.
- `prepare`: holds the target lock, records current image/config, validates an initial DB backup, builds using the pinned local runtime base, restores to isolated PostgreSQL/Redis on an internal network, and verifies startup twice. Resources belong only to that rehearsal and are cleaned afterward.
- `cutover`: holds the same target-wide lock, binds candidate/rollback IDs to preparation evidence, closes NewAPI ingress and the direct port, drains requests/batch updates, stops only the app, takes final database/data/log backups, updates the app, checks health/mounts/containers/data, then reopens ingress. No DB/Redis recreation, `down -v`, volume pruning or DB restoration.
- `verify`: deployed image and successful cutover, then public version and frontend entry asset hashes. An explicitly designated `--api-key-file /private/path` also checks `/v1/models`. No paid provider calls.
- `collect`: checksummed off-server recovery archive with final DB, roles, config, application data and audit metadata. Logs stay in remote `application-final.tgz`. The archive contains secrets and remains private; downloading is not restoration.

Only explicit system-task/instance metadata and Casbin ID rewrites with identical policy content/count are allowed startup mutations. Rehearsal and cutover use the same checks. Schema or unexpected business-data changes stop the release.

## Uncertain transport, failure and rollback

SSH timeout does not establish whether a write completed. Inspect first:

```sh
python3 ops/deploy/deploy.py status --target "$NEWAPI_TARGET" --release "$NEWAPI_RELEASE"
# Without local artifacts:
python3 ops/deploy/deploy.py status --target "$NEWAPI_TARGET" --release-id "$NEWAPI_RELEASE_ID"
```

Read remote `prepare-state.json` and backup `cutover-state.json`; detailed evidence stays private. Preparation/cutover cannot be blindly rerun after state exists. Check the live process and `ROOT/.newapi-deploy.lock`; never delete a lock file to bypass a running operation. `stage` can resume because it verifies immutable metadata/chunks. Checksum disagreement needs investigation, not overwriting.

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
