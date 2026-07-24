#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2020 Stalwart Labs LLC <hello@stalw.art>
#
# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-SEL
"""Rebuild resources/schema/schema.json.gz and its hash from the checked-in blob.

`schema.json.gz` is a generated artifact with no generator in this tree: it is
served verbatim by `GET /api/schema` (crates/http/src/api/mod.rs) and the admin
client caches it under the digest in `schema.json.sha256`. Two properties are
load-bearing:

  * `schema.json.sha256` is the base64url (unpadded, no trailing newline) SHA-256
    of the GZIP BYTES, and is `include_str!`d straight into the cache URL, which
    is served with an immutable cache header. A wrong value is not a transient
    error: clients pin the wrong document under that key.
  * The blob lists each enum's variants in DECLARATION order. Enum values cross
    the API as name strings (`impl Serialize for ExpressionConstant` is
    `serialize_str(self.as_str())`), so an entry's index is a display-ordering
    artifact, NOT a wire value -- `Permission` is proof: `jmapFileNodeCopy` has
    discriminant 659 and sits at blob index 95. For enums whose declaration order
    and discriminant order coincide (which is true of `ExpressionConstant`), the
    cheapest available drift check is that index == `from_id`, so this script
    appends rather than inserts and asserts that equality afterwards.

This script re-applies the entries in ADDITIONS to whatever blob is currently
checked out. It is idempotent, so the normal recipe after taking a new upstream
blob is simply to run it again.

  ./resources/schema/regenerate.py            # rewrite the blob + hash in place
  ./resources/schema/regenerate.py --check    # verify only; non-zero on drift
"""

from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import io
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BLOB = REPO_ROOT / "resources/schema/schema.json.gz"
HASH = REPO_ROOT / "resources/schema/schema.json.sha256"
ENUMS_RS = REPO_ROOT / "crates/registry/src/schema/enums.rs"
ENUMS_IMPL_RS = REPO_ROOT / "crates/registry/src/schema/enums_impl.rs"

# Entries this tree adds on top of the upstream blob, in append order.
# Each must have a matching Rust variant whose explicit discriminant equals the
# index the entry ends up at; that is checked below, not assumed. The equality is
# a drift detector for these specific enums, not a general property of the blob.
ADDITIONS: dict[str, list[dict[str, str]]] = {
    "ExpressionConstant": [
        {"name": "minutely", "label": ""},
    ],
}


class Drift(Exception):
    """An invariant the script refuses to paper over."""


def pascal_case(name: str) -> str:
    return "".join(part.capitalize() for part in name.split("_"))


def rust_discriminant(enum_name: str, variant_name: str) -> int:
    """Explicit discriminant of `variant_name` in `pub enum <enum_name>`."""
    src = ENUMS_RS.read_text()
    block = re.search(rf"pub enum {enum_name} \{{(.*?)\n\}}", src, re.S)
    if not block:
        raise Drift(f"{ENUMS_RS.name}: no `pub enum {enum_name}` block")
    variant = pascal_case(variant_name)
    hit = re.search(rf"^\s*{variant}\s*=\s*(\d+)\s*,", block.group(1), re.M)
    if not hit:
        raise Drift(
            f"{ENUMS_RS.name}: {enum_name}::{variant} missing or has no explicit "
            f"discriminant (discriminants are persisted; they must be explicit)"
        )
    return int(hit.group(1))


def rust_count(enum_name: str) -> int:
    """`const COUNT` from `impl EnumImpl for <enum_name>`."""
    src = ENUMS_IMPL_RS.read_text()
    block = re.search(rf"impl EnumImpl for {enum_name} \{{(.*?)\n\}}", src, re.S)
    if not block:
        raise Drift(f"{ENUMS_IMPL_RS.name}: no `impl EnumImpl for {enum_name}`")
    hit = re.search(r"const COUNT: usize = (\d+);", block.group(1))
    if not hit:
        raise Drift(f"{ENUMS_IMPL_RS.name}: {enum_name} has no `const COUNT`")
    return int(hit.group(1))


def dump(schema: dict) -> bytes:
    """Serialize exactly the way the upstream generator does."""
    return json.dumps(schema, separators=(",", ":"), ensure_ascii=False).encode()


def compress(plain: bytes) -> bytes:
    """Deterministic gzip: fixed mtime, level 9, no filename in the header."""
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=9, mtime=0) as fh:
        fh.write(plain)
    return buf.getvalue()


def digest(gz: bytes) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(gz).digest()).decode().rstrip("=")


def apply_additions(schema: dict, *, report: list[str]) -> dict:
    for enum_name, entries in ADDITIONS.items():
        enum_list = schema.get("enums", {}).get(enum_name)
        if enum_list is None:
            raise Drift(f"blob has no enums.{enum_name}; did upstream rename it?")

        for entry in entries:
            name = entry["name"]
            want = rust_discriminant(enum_name, name)
            at = next(
                (i for i, e in enumerate(enum_list) if e.get("name") == name), None
            )
            if at is None:
                # APPEND, so the blob keeps listing this enum in the same order
                # the Rust declaration does and the index == discriminant check
                # below stays meaningful.
                at = len(enum_list)
                enum_list.append(dict(entry))
                report.append(f"enums.{enum_name}: appended {name!r} at index {at}")
            elif enum_list[at] != entry:
                enum_list[at] = dict(entry)
                report.append(f"enums.{enum_name}: rewrote {name!r} at index {at}")

            if at != want:
                raise Drift(
                    f"enums.{enum_name}[{at}] = {name!r} but "
                    f"{enum_name}::{pascal_case(name)} = {want} in Rust. Upstream "
                    f"most likely appended variants of its own, which moved our "
                    f"append point. Renumber the Rust variant to {at} (discriminant "
                    f"and declaration position both) and re-run."
                )

        count = rust_count(enum_name)
        if count != len(enum_list):
            raise Drift(
                f"enums.{enum_name} has {len(enum_list)} entries but "
                f"`impl EnumImpl for {enum_name}` declares COUNT = {count}"
            )
    return schema


def build(current_gz: bytes, *, allow_reformat: bool, report: list[str]) -> bytes:
    plain = gzip.decompress(current_gz)
    schema = json.loads(plain)

    if dump(schema) != plain and not allow_reformat:
        raise Drift(
            "re-serializing the blob unchanged does not reproduce it byte for byte, "
            "so the upstream generator's JSON formatting has changed. Rewriting it "
            "with ours would churn the whole file. Inspect the diff, then re-run "
            "with --allow-reformat if it is genuinely just formatting."
        )

    return compress(dump(apply_additions(schema, report=report)))


def verify(gz: bytes, hash_text: str) -> None:
    """Check the on-disk pair the same way a cold reader would."""
    if hash_text != digest(gz):
        raise Drift("schema.json.sha256 does not match schema.json.gz")
    if hash_text != hash_text.strip():
        raise Drift(
            "schema.json.sha256 has leading/trailing whitespace; it is `include_str!`d "
            "into the /api/schema/<hash> URL verbatim"
        )
    schema = json.loads(gzip.decompress(gz))
    for enum_name, entries in ADDITIONS.items():
        enum_list = schema["enums"][enum_name]
        if len(enum_list) != rust_count(enum_name):
            raise Drift(f"enums.{enum_name} length != Rust COUNT")
        for entry in entries:
            at = rust_discriminant(enum_name, entry["name"])
            if at >= len(enum_list) or enum_list[at] != entry:
                raise Drift(
                    f"enums.{enum_name}[{at}] is not {entry!r} in the written blob"
                )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--check",
        action="store_true",
        help="verify only; exit 1 if the blob or hash would change",
    )
    ap.add_argument(
        "--allow-reformat",
        action="store_true",
        help="proceed even if upstream's JSON formatting no longer round-trips",
    )
    args = ap.parse_args()

    try:
        current_gz = BLOB.read_bytes()
        report: list[str] = []
        new_gz = build(current_gz, allow_reformat=args.allow_reformat, report=report)
        new_hash = digest(new_gz)

        # Idempotency: feeding the result back in must be a fixed point.
        again: list[str] = []
        if build(new_gz, allow_reformat=True, report=again) != new_gz or again:
            raise Drift(f"not idempotent; a second pass still reports {again}")

        changed = new_gz != current_gz or new_hash != HASH.read_text()
        if args.check:
            verify(current_gz, HASH.read_text())
            if changed:
                raise Drift("blob is stale; re-run without --check")
            print("schema blob and hash are up to date")
            return 0

        if changed:
            BLOB.write_bytes(new_gz)
            HASH.write_text(new_hash)  # no trailing newline, on purpose
        verify(BLOB.read_bytes(), HASH.read_text())

        for line in report:
            print(line)
        print(f"{'wrote' if changed else 'unchanged'} {BLOB.name} ({len(new_gz)} bytes)")
        print(f"{'wrote' if changed else 'unchanged'} {HASH.name} = {new_hash}")
        return 0
    except Drift as err:
        print(f"error: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
