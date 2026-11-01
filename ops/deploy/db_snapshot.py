#!/usr/bin/env python3
"""Read-only PostgreSQL content/schema fingerprints; never print table contents."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys


class SnapshotError(Exception):
    pass


def database_output(container, command):
    try:
        result = subprocess.run(["docker", "exec", container, *command],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                timeout=180, check=False)
        if result.returncode != 0:
            raise SnapshotError("PostgreSQL snapshot command failed")
        return result.stdout.decode("utf-8")
    except (OSError, UnicodeError, subprocess.TimeoutExpired):
        raise SnapshotError("PostgreSQL snapshot command could not complete") from None


def query(container, sql):
    return database_output(container, ["sh", "-c",
        'psql -X -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "$1"', "sh", sql])


def normalize_dump(schema, compact=False, omit_line=None):
    """Ignore dump framing only outside SQL literals and quoted bodies."""
    kept = []
    quote = None
    escape_string = False
    comment_depth = 0
    for line in schema.split("\n"):
        if quote is None and comment_depth == 0:
            if line == omit_line:
                continue
            if line.startswith(("--", "\\restrict ", "\\unrestrict ")) or (compact and not line.strip()):
                continue
        kept.append(line)
        i = 0
        while i < len(line):
            if comment_depth:
                if line.startswith("/*", i):
                    comment_depth += 1
                    i += 2
                elif line.startswith("*/", i):
                    comment_depth -= 1
                    i += 2
                else:
                    i += 1
            elif quote in ("'", '"'):
                if quote == "'" and escape_string and line[i] == "\\":
                    i += 2
                elif line.startswith(quote * 2, i):
                    i += 2
                elif line[i] == quote:
                    quote = None
                    i += 1
                else:
                    i += 1
            elif quote is not None:
                if line.startswith(quote, i):
                    i += len(quote)
                    quote = None
                else:
                    i += 1
            elif line.startswith("--", i):
                break
            elif line.startswith("/*", i):
                comment_depth = 1
                i += 2
            elif line[i] in ("'", '"'):
                quote = line[i]
                escape_string = (quote == "'" and i > 0 and line[i - 1] in "Ee"
                                 and (i < 2 or not (line[i - 2].isalnum() or line[i - 2] in "_$")))
                i += 1
            elif line[i] == "$" and (i == 0 or not (line[i - 1].isalnum() or line[i - 1] in "_$")):
                match = re.match(r"\$(?:[A-Za-z_][A-Za-z0-9_]*)?\$", line[i:])
                if match:
                    quote = match.group()
                    i += len(quote)
                else:
                    i += 1
            else:
                i += 1
    if quote is not None or comment_depth:
        raise SnapshotError("unterminated PostgreSQL schema literal or comment")
    return "\n".join(kept)


def usage_tags_schema(schema):
    """Remove only the reviewed GORM additions, retaining every other DDL byte.

    pg_dump emits a deterministic CREATE TABLE block and one-line CREATE INDEX.
    Unknown column/index shapes fail closed instead of normalizing arbitrary SQL.
    """
    match = re.search(r"CREATE TABLE public\.logs \(\n(.*?)\n\);", schema, re.DOTALL)
    if not match:
        return {"valid": False}
    lines = match.group(1).split("\n")
    names = ("usage_tag", "usage_ref")
    found = {}
    retained = []
    for line in lines:
        name = next((name for name in names if re.match(r'\s+"?' + name + r'"?\s', line)), None)
        if name:
            expected = "    " + name + " character varying(64) DEFAULT ''::character varying"
            if name in found or line.rstrip(",") != expected:
                return {"valid": False}
            found[name] = True
        else:
            retained.append(line)
    index = "CREATE INDEX idx_logs_usage_tag ON public.logs USING btree (usage_tag);"
    index_lines = [line for line in schema.split("\n") if re.match(r'CREATE .*INDEX "?idx_logs_usage_tag"?\s', line)]
    if index_lines not in ([], [index]) or bool(found) != bool(index_lines) or set(found) not in (set(), set(names)):
        return {"valid": False}
    if found:
        if not retained:
            return {"valid": False}
        retained[-1] = retained[-1].removesuffix(",")
        schema = schema[:match.start(1)] + "\n".join(retained) + schema[match.end(1):]
        schema = "\n".join(line for line in schema.split("\n") if line != index)
    # Added object sections introduce empty lines. Preserve blanks inside SQL
    # strings/identifiers/dollar-quoted bodies: they can change actual defaults.
    normalized = normalize_dump(schema, compact=True)
    return {"valid": True, "present": bool(found),
            "base_schema_sha256": hashlib.sha256(normalized.encode()).hexdigest()}


def usage_tag_time_index_schema(schema):
    """Permit only the space/time index on an already-tagged logs table."""
    usage = usage_tags_schema(schema)
    if not usage.get("valid") or not usage.get("present"):
        return {"valid": False}
    index = "CREATE INDEX idx_logs_usage_tag_created_at ON public.logs USING btree (usage_tag, created_at);"
    index_lines = [line for line in schema.split("\n")
                   if re.match(r'CREATE .*INDEX "?idx_logs_usage_tag_created_at"?\s', line)]
    if index_lines not in ([], [index]):
        return {"valid": False}
    # Only omit an actual top-level DDL line, never identical text within a
    # multiline default/function body. Everything else must remain identical.
    normalized = normalize_dump(schema, compact=True, omit_line=index)
    present = normalized != normalize_dump(schema, compact=True)
    return {"valid": True, "present": present,
            "base_schema_sha256": hashlib.sha256(normalized.encode()).hexdigest()}


def capture_snapshot(container):
    tables = query(container, "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename;").splitlines()
    rows = {}
    for table in tables:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table):
            raise SnapshotError("unsupported PostgreSQL table name")
        sql = ('SELECT count(*),coalesce(md5(string_agg(h,\'\' ORDER BY h)),md5(\'\')) '
               'FROM (SELECT md5(to_jsonb(t)::text) h FROM "' + table + '" t) s;')
        count, digest = query(container, sql).strip().split("|")
        rows[table] = {"count": int(count), "sha": digest}
    schema = database_output(container, ["sh", "-c",
        'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --schema-only --no-owner --no-privileges'])
    schema = normalize_dump(schema)
    result = {"tables": rows, "schema_sha256": hashlib.sha256(schema.encode()).hexdigest()}
    if "logs" in tables:
        usage = usage_tags_schema(schema)
        count, digest, nonempty = query(container,
            "SELECT count(*),coalesce(md5(string_agg(h,'' ORDER BY h)),md5('')),"
            "count(*) FILTER (WHERE nonempty) FROM (SELECT "
            "md5((to_jsonb(t)-'usage_tag'-'usage_ref')::text) h, "
            "((to_jsonb(t)->>'usage_tag') IS DISTINCT FROM '' OR "
            "(to_jsonb(t)->>'usage_ref') IS DISTINCT FROM '') nonempty FROM public.logs t) s;"
        ).strip().split("|")
        usage.update(base_logs={"count": int(count), "sha": digest}, empty_values=int(nonempty) == 0)
        result["usage_tags_v1"] = usage
        result["usage_tag_time_index_v1"] = usage_tag_time_index_schema(schema)
    if "casbin_rule" in tables:
        policies = json.loads(query(container, "SELECT coalesce(json_agg(to_jsonb(t)-'id'),'[]'::json) FROM casbin_rule t;"))
        policy_json = json.dumps(sorted(policies, key=lambda row: json.dumps(row, sort_keys=True)), sort_keys=True)
        result["casbin_policy_sha256"] = hashlib.sha256(policy_json.encode()).hexdigest()
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("container")
    parser.add_argument("output", type=Path)
    args = parser.parse_args(argv)
    os.umask(0o077)
    try:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", args.container):
            raise SnapshotError("invalid PostgreSQL container name")
        if not args.output.is_absolute() or args.output.resolve() != args.output or args.output.exists():
            raise SnapshotError("snapshot requires a new absolute output path")
        result = capture_snapshot(args.container)
        with args.output.open("x", encoding="utf-8") as output:
            json.dump(result, output, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        print(json.dumps({"snapshot": args.output.name, "tables": len(result["tables"]),
                          "schema_sha256": result["schema_sha256"]}))
        return 0
    except Exception as error:
        message = str(error) if isinstance(error, SnapshotError) else type(error).__name__
        print(json.dumps({"error": "snapshot_failed", "reason": message}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
