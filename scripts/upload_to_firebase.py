"""Safely upload the local SQLite demo data to Firestore.

Preview first (default):
    python scripts/upload_to_firebase.py --credentials firebase-service-account.json
Apply missing documents:
    python scripts/upload_to_firebase.py --credentials firebase-service-account.json --apply
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

try:
    from google.cloud import firestore
    from google.oauth2 import service_account
except ImportError as exc:
    raise SystemExit("Thiếu thư viện Firestore. Chạy: python -m pip install -r requirements.txt") from exc


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "rooms.db"
DEVICE_COLUMNS = {"lamp": "light_on", "fan": "fan_on", "speaker": "speaker_on"}
AMENITIES = {
    "lamp": ("Đèn", "Điều khiển đèn trong phòng."),
    "fan": ("Quạt", "Điều khiển quạt trong phòng."),
    "speaker": ("Loa", "Phát âm thanh trong phòng."),
}
DEMO_USERS = {
    "demo-user": {"user_id": "demo-user", "username": "user", "role": "user", "name": "Người dùng demo", "demo": True},
    "demo-admin": {"user_id": "demo-admin", "username": "admin", "role": "admin", "name": "Quản trị viên", "demo": True},
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Xem trước hoặc tải dữ liệu SQLite lên Firestore.")
    parser.add_argument("--sqlite", type=Path, default=DEFAULT_DB, help="Đường dẫn database SQLite (mặc định: rooms.db).")
    parser.add_argument("--credentials", type=Path, help="Service account JSON; có thể dùng GOOGLE_APPLICATION_CREDENTIALS.")
    parser.add_argument("--project", help="Firebase/GCP project ID; mặc định lấy từ key JSON hoặc FIREBASE_PROJECT_ID.")
    parser.add_argument("--apply", action="store_true", help="Ghi các document chưa có lên Firestore. Mặc định chỉ xem trước.")
    parser.add_argument("--overwrite", action="store_true", help="Ghi đè document cùng ID; chỉ dùng khi chủ động đồng bộ SQLite lên Firestore.")
    return parser.parse_args()


def connect_firestore(args: argparse.Namespace):
    inline = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON", "").strip()
    credential_path = args.credentials or os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
    if inline:
        try:
            info = json.loads(inline)
        except json.JSONDecodeError as exc:
            raise SystemExit("FIREBASE_SERVICE_ACCOUNT_JSON không phải JSON hợp lệ.") from exc
        credentials = service_account.Credentials.from_service_account_info(info)
    elif credential_path:
        credential_path = Path(credential_path).expanduser()
        if not credential_path.is_file():
            raise SystemExit(f"Không tìm thấy file service account: {credential_path}")
        credentials = service_account.Credentials.from_service_account_file(str(credential_path))
        info = json.loads(credential_path.read_text(encoding="utf-8"))
    else:
        raise SystemExit("Chưa có credential. Truyền --credentials <file.json> hoặc đặt GOOGLE_APPLICATION_CREDENTIALS.")
    project_id = args.project or os.getenv("FIREBASE_PROJECT_ID") or info.get("project_id")
    if not project_id:
        raise SystemExit("Không tìm thấy project_id trong key JSON. Truyền --project hoặc đặt FIREBASE_PROJECT_ID.")
    return firestore.Client(project=project_id, credentials=credentials), project_id


def table_rows(connection: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
    present = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
    if not present:
        return []
    return [dict(row) for row in connection.execute(f'SELECT * FROM "{table}"').fetchall()]


def read_local_data(path: Path) -> dict[str, list[tuple[str, dict[str, Any]]]]:
    if not path.is_file():
        raise SystemExit(f"Không tìm thấy database SQLite: {path}\nHãy chạy app ít nhất một lần hoặc truyền đúng đường dẫn --sqlite.")
    connection = sqlite3.connect(str(path))
    connection.row_factory = sqlite3.Row
    try:
        rooms = table_rows(connection, "rooms")
        bookings = table_rows(connection, "bookings")
        events = table_rows(connection, "events")
        audit = table_rows(connection, "audit")
        devices = table_rows(connection, "devices")
        amenities = table_rows(connection, "amenities")
        links = table_rows(connection, "room_amenities")
    finally:
        connection.close()

    amenity_by_id = {str(item["id"]): item for item in amenities}
    # Use stable canonical IDs in Firestore; older SQLite databases used integer IDs.
    amenity_ids = {name.casefold(): key for key, (name, _) in AMENITIES.items()}
    room_links: dict[str, list[str]] = {}
    for link in links:
        raw_amenity = amenity_by_id.get(str(link.get("amenity_id")), {})
        canonical_id = amenity_ids.get(str(raw_amenity.get("name", "")).casefold())
        if canonical_id:
            room_links.setdefault(str(link["room_id"]), []).append(canonical_id)

    output: dict[str, list[tuple[str, dict[str, Any]]]] = {name: [] for name in ("rooms", "bookings", "audit", "app_meta")}
    catalog_items = {}
    for amenity_id, (name, description) in AMENITIES.items():
        raw = next((item for item in amenities if str(item.get("name", "")).casefold() == name.casefold()), {})
        catalog_items[amenity_id] = {"name": name, "description": description, "enabled": bool(raw.get("enabled", True))}
    output["app_meta"].append(("amenities", {"items": catalog_items}))
    for raw in rooms:
        room_id = int(raw["id"])
        room = {key: raw.get(key) for key in (
            "id", "name", "capacity", "hourly_rate", "online", "occupied",
            "light_on", "fan_on", "speaker_on", "last_seen",
        ) if key in raw}
        room.setdefault("online", True)
        for key in ("online", "occupied", "light_on", "fan_on", "speaker_on"):
            room[key] = bool(room.get(key, key == "online"))
        room["amenity_ids"] = sorted(set(room_links.get(str(room_id), [])))
        room["device_config"] = {}
        for device, column in DEVICE_COLUMNS.items():
            config = next((item for item in devices if int(item["room_id"]) == room_id and item["device"] == device), {})
            room["device_config"][device] = {"name": config.get("name") or AMENITIES[device][0], "enabled": bool(config.get("enabled", True))}
        output["rooms"].append((str(room_id), room))
    output["app_meta"].extend([
        ("render_bootstrap_v1", {"version": 1, "source": "sqlite_upload"}),
        ("sqlite_initial_upload_v1", {"version": 1, "source": "sqlite_upload"}),
        ("amenity_catalog_v4", {"version": 4, "source": "sqlite_upload"}),
        ("all_rooms_have_all_amenities_v3", {"version": 3, "source": "sqlite_upload"}),
        ("compact_schema_v1", {"version": 1, "source": "sqlite_upload"}),
    ])

    events_by_booking: dict[str, list[dict[str, Any]]] = {}
    for row in events:
        events_by_booking.setdefault(str(row.get("booking_id", "")), []).append({
            "event": row.get("event"), "at": row.get("at"), "detail": row.get("detail", "")
        })
    for booking in bookings:
        booking_id = str(booking["id"])
        payload = dict(booking)
        payload.pop("id", None)
        payload["events"] = events_by_booking.get(booking_id, [])
        output["bookings"].append((booking_id, payload))
    for collection, rows in (("audit", audit),):
        for row in rows:
            document_id = str(row.get("id") or hashlib.sha256(json.dumps(row, sort_keys=True, default=str).encode()).hexdigest())
            payload = dict(row)
            payload.pop("id", None)
            output[collection].append((document_id, payload))
    return output


def upload(client, records: dict[str, list[tuple[str, dict[str, Any]]]], apply: bool, overwrite: bool) -> None:
    counts = {}
    room_summaries_synced = 0
    for collection_name, documents in records.items():
        collection = client.collection(collection_name)
        existing_ids = {snapshot.id for snapshot in collection.stream()} if not overwrite else set()
        new_documents = [(doc_id, payload) for doc_id, payload in documents if overwrite or doc_id not in existing_ids]
        counts[collection_name] = (len(documents), len(new_documents), len(documents) - len(new_documents))
        if not apply:
            continue
        if collection_name == "rooms" and not overwrite:
            # Keep cloud-managed settings and live state, while updating only the
            # room's canonical amenity ID list.
            for doc_id, payload in documents:
                if doc_id in existing_ids:
                    collection.document(doc_id).set({
                        "amenity_ids": payload.get("amenity_ids", []),
                        "device_config": payload.get("device_config", {}),
                        "amenities": firestore.DELETE_FIELD,
                    }, merge=True)
                    room_summaries_synced += 1
        if not new_documents:
            continue
        for offset in range(0, len(new_documents), 450):
            batch = client.batch()
            for doc_id, payload in new_documents[offset:offset + 450]:
                batch.set(collection.document(doc_id), payload, merge=not overwrite)
            batch.commit()

    print("Firestore upload plan:")
    print(f"  {'Collection':<20} {'Local':>7} {'To write':>10} {'Skipped':>9}")
    for name, (local_count, write_count, skipped_count) in counts.items():
        print(f"  {name:<20} {local_count:>7} {write_count:>10} {skipped_count:>9}")
    if apply:
        print("Upload complete. Existing documents were skipped." if not overwrite else "Upload complete. Matching documents were overwritten.")
        if not overwrite:
            print(f"Room amenity summaries synchronized: {room_summaries_synced}")
    else:
        print("Preview only: no documents were changed. Add --apply to upload missing documents.")


def main() -> int:
    args = parse_args()
    try:
        records = read_local_data(args.sqlite)
        client, project_id = connect_firestore(args)
        # Confirm the target can be reached before displaying a plan or writing.
        next(iter(client.collection("rooms").limit(1).stream()), None)
        print(f"Source: {args.sqlite.resolve()}\nTarget project: {project_id}")
        upload(client, records, args.apply, args.overwrite)
        return 0
    except SystemExit:
        raise
    except Exception as exc:
        print(f"Upload failed ({type(exc).__name__}): {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
