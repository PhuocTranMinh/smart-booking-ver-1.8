"""Compact duplicate legacy state while retaining normalized Firestore entities.

Preview only:
  python scripts/simplify_firestore.py --credentials firebase-service-account.json
Apply (creates a complete JSON backup first):
  python scripts/simplify_firestore.py --credentials firebase-service-account.json --apply --backup firestore-before-compact.json
"""
from __future__ import annotations

import argparse
import hashlib
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
DEFAULT_AMENITY_RATES = {"lamp": 2000, "fan": 3000, "speaker": 5000}
LEGACY_COLLECTIONS = ("room_status", "events")
PRESERVED_COLLECTIONS = (
    "amenities", "room_amenities", "devices", "booking_events", "idempotency",
    "users", "qr_tokens", "device_logs",
)


def event_key(row: dict[str, Any]) -> tuple[str, str, str]:
    return (str(row.get("event", "")), str(row.get("at", "")), str(row.get("detail", "")))


def main() -> int:
    parser = argparse.ArgumentParser(description="Chuẩn hóa Firestore, giữ các collection theo mô hình booking và IoT.")
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
        for name in (*LEGACY_COLLECTIONS, *PRESERVED_COLLECTIONS, "rooms", "bookings", "audit", "app_meta")
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

    booking_ids = {snapshot.id for snapshot in booking_docs}
    booking_events_by_id: dict[str, list[dict[str, Any]]] = {}
    for source in (docs["booking_events"], docs["events"]):
        for doc in source:
            row = doc.to_dict() or {}
            booking_id = str(row.get("booking_id", ""))
            if booking_id and booking_id in booking_ids:
                booking_events_by_id.setdefault(booking_id, []).append({
                    "booking_id": booking_id,
                    "event": row.get("event", ""),
                    "at": row.get("at", ""),
                    "detail": row.get("detail", ""),
                })
    booking_updates: dict[str, dict[str, Any]] = {}
    normalized_booking_events: dict[str, dict[str, Any]] = {}
    normalized_idempotency: dict[str, dict[str, Any]] = {}
    for doc in booking_docs:
        booking = doc.to_dict() or {}
        embedded_events = booking.get("events", [])
        events = [dict(event, booking_id=doc.id) for event in embedded_events if isinstance(event, dict)] if isinstance(embedded_events, list) else []
        known = {event_key(event) for event in events}
        for event in booking_events_by_id.get(doc.id, []):
            key = event_key(event)
            if key not in known:
                events.append(event)
                known.add(key)
        for event in events:
            normalized = {
                "booking_id": doc.id,
                "event": str(event.get("event", "")),
                "at": str(event.get("at", "")),
                "detail": str(event.get("detail", "")),
            }
            event_id = hashlib.sha256(json.dumps(normalized, sort_keys=True).encode()).hexdigest()[:40]
            normalized_booking_events[event_id] = normalized
        old_idempotency_key = str(booking.get("idempotency_key") or "")
        if old_idempotency_key:
            key_hash = hashlib.sha256(old_idempotency_key.encode()).hexdigest()
            normalized_idempotency[key_hash] = {
                "idempotency_key_hash": key_hash, "booking_id": doc.id,
                "created_at": str(booking.get("created", "")),
            }
        booking_updates[doc.id] = {
            "events": firestore.DELETE_FIELD,
            "idempotency_key": firestore.DELETE_FIELD,
        }

    catalog_items = {}
    for key, default in AMENITIES.items():
        old = old_catalog.get(key) or catalog_old.get(key) or {}
        catalog_items[key] = {
            **default,
            "enabled": bool(old.get("enabled", True)),
            "hourly_rate": int(old.get("hourly_rate", DEFAULT_AMENITY_RATES[key])),
        }

    print(f"Firestore project: {args.project}")
    print(f"Core documents: rooms ({len(room_docs)}), bookings ({len(booking_docs)}), audit ({len(docs['audit'])})")
    print("Redundant collections that will be removed:")
    for name in LEGACY_COLLECTIONS:
        print(f"  {name}: {len(docs[name])} documents")
    print(f"Room documents to normalize: {len(room_updates)}; booking events: {len(normalized_booking_events)}; idempotency keys: {len(normalized_idempotency)}")
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

    batch = client.batch()
    writes = 0
    for key, item in catalog_items.items():
        batch.set(client.collection("amenities").document(key), item, merge=True)
        writes += 1
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
    for event_id, event in normalized_booking_events.items():
        batch.set(client.collection("booking_events").document(event_id), event, merge=True)
        writes += 1
        if writes >= 400:
            batch.commit()
            batch = client.batch()
            writes = 0
    for key_hash, result in normalized_idempotency.items():
        batch.set(client.collection("idempotency").document(key_hash), result, merge=True)
        writes += 1
        if writes >= 400:
            batch.commit()
            batch = client.batch()
            writes = 0
    if writes:
        batch.commit()

    for key in AMENITIES:
        saved = client.collection("amenities").document(key).get()
        if not saved.exists or not (saved.to_dict() or {}).get("name"):
            raise RuntimeError(f"Amenity migration verification failed for amenities/{key}; legacy data was kept")
    client.collection("app_meta").document("amenities").delete()

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
        "kept_collections": ["rooms", "bookings", "users", "amenities", "room_amenities", "devices", "qr_tokens", "booking_events", "device_logs", "idempotency", "audit", "app_meta"],
        "backup_file": str(backup_path),
    })
    print(f"Compaction complete. Backup written to: {backup_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
