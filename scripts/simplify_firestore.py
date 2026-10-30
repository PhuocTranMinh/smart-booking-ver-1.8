"""Compact legacy Firestore data into rooms, bookings, audit, and app_meta.

Preview only:
  python scripts/simplify_firestore.py --credentials firebase-service-account.json
Apply (creates a complete JSON backup first):
  python scripts/simplify_firestore.py --credentials firebase-service-account.json --apply --backup firestore-before-compact.json
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from google.cloud import firestore
from google.oauth2 import service_account


AMENITIES = {
    "lamp": {"name": "Đèn", "description": "Điều khiển đèn trong phòng."},
    "fan": {"name": "Quạt", "description": "Điều khiển quạt trong phòng."},
    "speaker": {"name": "Loa", "description": "Phát âm thanh trong phòng."},
}
DEVICE_COLUMNS = {"lamp": "light_on", "fan": "fan_on", "speaker": "speaker_on"}
LEGACY_COLLECTIONS = (
    "amenities", "room_amenities", "room_status", "devices",
    "booking_events", "idempotency", "users", "events",
)


def event_key(row: dict[str, Any]) -> tuple[str, str, str]:
    return (str(row.get("event", "")), str(row.get("at", "")), str(row.get("detail", "")))


def main() -> int:
    parser = argparse.ArgumentParser(description="Gom Firestore về 4 collection cốt lõi.")
    parser.add_argument("--credentials", required=True, help="Đường dẫn JSON service account")
    parser.add_argument("--project", default="smart-booking-82438")
    parser.add_argument("--apply", action="store_true", help="Ghi dữ liệu gộp và xóa collection cũ")
    parser.add_argument("--backup", help="File JSON backup trước khi ghi/xóa; bắt buộc cùng --apply")
    args = parser.parse_args()
    if args.apply and not args.backup:
        parser.error("Khi dùng --apply, cần --backup để lưu bản dữ liệu trước khi thay đổi")

    credentials = service_account.Credentials.from_service_account_file(args.credentials)
    client = firestore.Client(project=args.project, credentials=credentials)
    docs: dict[str, list[Any]] = {
        name: list(client.collection(name).stream())
        for name in (*LEGACY_COLLECTIONS, "rooms", "bookings", "audit", "app_meta")
    }
    room_docs = docs["rooms"]
    booking_docs = docs["bookings"]
    room_by_id = {doc.id: doc for doc in room_docs}
    catalog_old = {doc.id: (doc.to_dict() or {}) for doc in docs["amenities"]}
    meta_docs = {doc.id: (doc.to_dict() or {}) for doc in docs["app_meta"]}
    old_catalog = (meta_docs.get("amenities") or {}).get("items", {})

    names_to_ids = {value["name"].casefold(): key for key, value in AMENITIES.items()}
    names_to_ids.update({"light": "lamp", "lamp": "lamp", "fan": "fan", "speaker": "speaker"})

    def canonical_amenity(value: Any) -> str | None:
        raw = str(value)
        if raw in AMENITIES:
            return raw
        item = old_catalog.get(raw) or catalog_old.get(raw) or {}
        name = str(item.get("name", raw)).casefold()
        return names_to_ids.get(name)

    links_by_room: dict[str, set[str]] = {}
    for link_doc in docs["room_amenities"]:
        row = link_doc.to_dict() or {}
        room_id = str(row.get("room_id", link_doc.id.split("_", 1)[0]))
        key = str(row.get("amenity_id", ""))
        legacy = old_catalog.get(key) or catalog_old.get(key) or {}
        canonical = key if key in AMENITIES else names_to_ids.get(str(legacy.get("name", key)).casefold())
        if canonical:
            links_by_room.setdefault(room_id, set()).add(canonical)

    status_by_room = {doc.id: (doc.to_dict() or {}) for doc in docs["room_status"]}
    devices_by_room: dict[str, dict[str, dict[str, Any]]] = {}
    for doc in docs["devices"]:
        row = doc.to_dict() or {}
        room_id, device = str(row.get("room_id", "")), str(row.get("device", ""))
        if room_id and device in AMENITIES:
            devices_by_room.setdefault(room_id, {})[device] = row

    room_updates: dict[str, dict[str, Any]] = {}
    for doc in room_docs:
        room = doc.to_dict() or {}
        ids = {key for raw in room.get("amenity_ids", []) if (key := canonical_amenity(raw))}
        ids.update(links_by_room.get(doc.id, set()))
        if not ids:
            for label in room.get("amenities", []):
                key = canonical_amenity(label)
                if key:
                    ids.add(key)
        if not ids:
            ids = set(AMENITIES)
        status = status_by_room.get(doc.id, {})
        updates: dict[str, Any] = {
            "amenity_ids": sorted(ids),
            "online": bool(status.get("online", room.get("online", True))),
            "occupied": bool(status.get("occupied", room.get("occupied", False))),
            "last_seen": status.get("last_seen", room.get("last_seen")),
        }
        status_devices = status.get("devices", {})
        for device, column in DEVICE_COLUMNS.items():
            updates[column] = bool(status_devices.get(device, room.get(column, False)))
        existing_config = room.get("device_config", {})
        config = {}
        for device, label in (("lamp", "Đèn"), ("fan", "Quạt"), ("speaker", "Loa")):
            old = devices_by_room.get(doc.id, {}).get(device, {})
            current = existing_config.get(device, {})
            config[device] = {
                "name": current.get("name", old.get("name", label)),
                "enabled": bool(current.get("enabled", old.get("enabled", True))),
            }
        updates["device_config"] = config
        updates["amenities"] = firestore.DELETE_FIELD
        room_updates[doc.id] = updates

    booking_events_by_id: dict[str, list[dict[str, Any]]] = {}
    for source in (docs["booking_events"], docs["events"]):
        for doc in source:
            row = doc.to_dict() or {}
            booking_id = str(row.get("booking_id", ""))
            if booking_id and booking_id in {snapshot.id for snapshot in booking_docs}:
                booking_events_by_id.setdefault(booking_id, []).append({
                    "booking_id": booking_id,
                    "event": row.get("event", ""),
                    "at": row.get("at", ""),
                    "detail": row.get("detail", ""),
                })
    booking_updates: dict[str, dict[str, Any]] = {}
    for doc in booking_docs:
        booking = doc.to_dict() or {}
        events = list(booking.get("events", []))
        known = {event_key(event) for event in events if isinstance(event, dict)}
        for event in booking_events_by_id.get(doc.id, []):
            key = event_key(event)
            if key not in known:
                events.append(event)
                known.add(key)
        booking_updates[doc.id] = {"events": events}

    catalog_items = {}
    for key, default in AMENITIES.items():
        old = old_catalog.get(key) or catalog_old.get(key) or {}
        catalog_items[key] = {
            **default,
            "enabled": bool(old.get("enabled", True)),
        }

    print(f"Firestore project: {args.project}")
    print(f"Canonical collections to keep: rooms ({len(room_docs)}), bookings ({len(booking_docs)}), audit ({len(docs['audit'])}), app_meta ({len(docs['app_meta']) + 1})")
    print("Legacy collections that will be removed:")
    for name in LEGACY_COLLECTIONS:
        print(f"  {name}: {len(docs[name])} documents")
    print(f"Room documents to normalize: {len(room_updates)}; booking histories to embed: {len(booking_updates)}")
    if not args.apply:
        print("Preview only. No Firestore data was changed.")
        print("After deploying the updated app, rerun with --apply --backup <new-json-file>.")
        return 0

    backup_path = Path(args.backup).expanduser().resolve()
    if backup_path.exists():
        parser.error(f"Backup file already exists; choose a new path: {backup_path}")
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    backup = {
        "project": args.project,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "collections": {
            name: [{"id": doc.id, "data": doc.to_dict() or {}} for doc in rows]
            for name, rows in docs.items()
        },
    }
    backup_path.write_text(json.dumps(backup, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    meta_ref = client.collection("app_meta").document("amenities")
    meta_ref.set({"items": catalog_items})
    batch = client.batch()
    writes = 0
    for doc_id, updates in room_updates.items():
        batch.set(room_by_id[doc_id].reference, updates, merge=True)
        writes += 1
        if writes >= 400:
            batch.commit()
            batch = client.batch()
            writes = 0
    for doc_id, updates in booking_updates.items():
        batch.set(client.collection("bookings").document(doc_id), updates, merge=True)
        writes += 1
        if writes >= 400:
            batch.commit()
            batch = client.batch()
            writes = 0
    if writes:
        batch.commit()

    # Verify compact fields before deleting any legacy collection documents.
    for doc_id in room_updates:
        stored = room_by_id[doc_id].reference.get().to_dict() or {}
        if not stored.get("amenity_ids") or "device_config" not in stored:
            raise RuntimeError(f"Room migration verification failed for rooms/{doc_id}; old collections were left intact")
    for name in LEGACY_COLLECTIONS:
        refs = [doc.reference for doc in docs[name]]
        for offset in range(0, len(refs), 450):
            delete_batch = client.batch()
            chunk = refs[offset:offset + 450]
            for ref in chunk:
                delete_batch.delete(ref)
            if chunk:
                delete_batch.commit()
    client.collection("app_meta").document("compact_schema_v1").set({
        "version": 1,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "kept_collections": ["rooms", "bookings", "audit", "app_meta"],
        "backup_file": str(backup_path),
    })
    print(f"Compaction complete. Backup written to: {backup_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
