"""Re-key Firestore audit events so the Console's document-ID order follows time."""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from google.cloud import firestore
from google.oauth2 import service_account


def parse_at(value):
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str) and value.strip():
        try:
            result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--credentials", required=True, help="Firebase service-account JSON")
    parser.add_argument("--project", required=True, help="Firebase project ID")
    parser.add_argument("--apply", action="store_true", help="Write changes; default is preview")
    parser.add_argument("--backup", default="firestore-audit-before-sort.json")
    args = parser.parse_args()

    credentials = service_account.Credentials.from_service_account_file(args.credentials)
    client = firestore.Client(project=args.project, credentials=credentials)
    collection = client.collection("audit")
    snapshots = list(collection.stream())

    moves = []
    for snapshot in snapshots:
        data = snapshot.to_dict() or {}
        parsed = parse_at(data.get("at"))
        if not parsed:
            print(f"SKIP {snapshot.id}: missing/invalid at")
            continue
        expected_prefix = parsed.strftime("%Y%m%dT%H%M%S%fZ_")
        if snapshot.id.startswith(expected_prefix):
            continue
        suffix = hashlib.sha256(snapshot.id.encode("utf-8")).hexdigest()[:10]
        moves.append((snapshot, expected_prefix + suffix, data))

    print(f"Audit docs: {len(snapshots)}; IDs to sort: {len(moves)}")
    for old, new, _ in sorted(moves, key=lambda item: item[1])[:10]:
        print(f"{old.id} -> {new}")
    if not args.apply:
        print("Preview only. Add --apply to back up and sort the audit document IDs.")
        return 0

    backup = Path(args.backup).resolve()
    if backup.exists():
        raise SystemExit(f"Backup already exists; choose another path: {backup}")
    backup.parent.mkdir(parents=True, exist_ok=True)
    backup.write_text(
        json.dumps(
            [{"id": snapshot.id, "data": snapshot.to_dict() or {}} for snapshot in snapshots],
            ensure_ascii=False,
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )
    print(f"Backup written: {backup}")

    # Create all time-keyed copies before removing legacy IDs, making an interrupted
    # migration safe to resume without losing the original documents.
    for start in range(0, len(moves), 400):
        batch = client.batch()
        for _, new_id, data in moves[start:start + 400]:
            batch.set(collection.document(new_id), data)
        batch.commit()
    for start in range(0, len(moves), 400):
        batch = client.batch()
        for old_snapshot, _, _ in moves[start:start + 400]:
            batch.delete(old_snapshot.reference)
        batch.commit()

    print(f"Done. Re-keyed {len(moves)} audit events; fields and event data were preserved.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
