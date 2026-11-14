# Backup and recovery

What has to survive: the **PostgreSQL database** (projects, requirements, approvals, the audit
log, workflow checkpoints, the job queue) and the **data directory** (`/data`: uploaded
documents, local repositories and their mirrors, release artifacts). The two refer to each
other, so they are backed up and restored together. Configuration lives in your values file
and secrets, which you keep in your own configuration management.

Targets for a standard installation: **RPO 24 h** with the nightly backup alone, **RPO minutes**
with PostgreSQL point-in-time recovery plus volume snapshots; **RTO about 1 hour** for a
restore into a new cluster.

## Taking a backup

```bash
agentic-sdlc backup --out /backups          # in any API or worker pod, or the CronJob
agentic-sdlc verify-backup /backups/asw-backup-20261113T021700Z.tar
```

A backup is one tar file: a `pg_dump` of the database, the data directory, and a manifest with
the app version, the schema revision and SHA-256 checksums. The database is dumped first and
the files copied after it; stored objects are only ever added, so every file the database
refers to is in the archive. The file is written as owner-only and appears only when complete.

In Kubernetes, enable the CronJob (`backup.enabled: true`). It writes to its own volume and
deletes backups older than `backup.retentionDays`. **Copy that volume off the cluster** (Velero,
a storage snapshot policy, or `rclone` to object storage with versioning and object lock): a
backup on the same storage as the data does not survive losing that storage. Encrypt at rest
on that storage; the archive holds everything, including the audit log.

For a smaller RPO, also use your database's point-in-time recovery and snapshot the `/data`
volume on the same schedule.

## Restoring

Restore into a **fresh** installation (new database, empty `/data`) whenever you can.

1. Install the chart with `api.replicas: 0` and `worker.replicas: 0`, using the **same release**
   that made the backup (the manifest's `app_version`). Nothing should write while you restore.
2. Copy the backup into a pod that has `/data` mounted (or run a one-off pod of the API image
   with the data volume and the secret), then:

   ```bash
   agentic-sdlc verify-backup asw-backup-....tar
   agentic-sdlc restore asw-backup-....tar
   alembic upgrade head
   ```

   Restore checks the checksums before touching anything, refuses a backup made by a newer
   schema than this code knows, and refuses a target that already has tables or files.
   `--force` replaces an existing installation: the database schema is dropped and recreated,
   and `/data` is emptied first, so nothing from the old state is mixed in.
3. Scale the API and workers back up. Workflow runs resume from their last checkpoint; jobs
   that were leased when the backup was taken are picked up again after their lease expires.
4. Check `/api/v1/ready`, sign in, open a project and its latest run, and look at the audit log.

## Disaster recovery drill

Run this at least every quarter, and after any change to storage or the database:

1. Take a backup in production and copy it to the recovery environment.
2. Restore it into a fresh namespace or cluster as above.
3. Compare counts: `projects`, `workflow_runs`, `audit_events` match the source at backup time.
4. Open a document and a release evidence bundle (proves `/data` came back).
5. Record the time taken (your measured RTO) and any problem in the drill log.

The test suite does this on every CI run (`tests/integration/test_backup.py`): it backs up a
database with real content and a data directory, restores both into a fresh database, compares
every table's row count, and checks that damaged, foreign and newer-schema backups are refused
without changing anything.

## Repository mirrors

Mirrors of remote repositories (`/data/repositories/<id>.git`) are caches. If they are lost,
"Sync" on the repository (or the next run) fetches them again from the git host. Local
repositories created in the platform (`/data/repositories/<id>/`) are the only copy: they are
in the backup.
