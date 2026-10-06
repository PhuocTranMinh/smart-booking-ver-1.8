from __future__ import annotations

import base64
import contextvars
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import sqlite3
import threading
import urllib.request
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional, Union

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

try:
    import paho.mqtt.client as mqtt
except ImportError:
    mqtt = None

try:
    from google.cloud import firestore
    from google.oauth2 import service_account
except ImportError:
    firestore = None
    service_account = None

BASE = Path(__file__).parent
DB_PATH = Path(os.getenv("ROOM_DB", str(BASE / "rooms.db")))
SECRET = os.getenv("QR_SECRET", "demo-only-change-this-secret").encode()
LOCK = threading.RLock()
CHAT: dict[str, dict[str, Any]] = {}
AI_CHAT: dict[str, list[dict[str, str]]] = {}
CHAT_REQUEST_MESSAGE = contextvars.ContextVar("chat_request_message", default=None)
SESSIONS: dict[str, dict[str, str]] = {}
MQTT_CLIENT = None
MQTT_CONNECTED = False
FIRESTORE = None
FIREBASE_PROJECT_ID = os.getenv("FIREBASE_PROJECT_ID", "smart-booking-82438")
ROOM_AMENITIES = {
    "lamp": ("Đèn", "Điều khiển đèn trong phòng."),
    "fan": ("Quạt", "Điều khiển quạt trong phòng."),
    "speaker": ("Loa", "Phát âm thanh trong phòng."),
}
LIFECYCLE_STOP = threading.Event()
LIFECYCLE_THREAD = None
app = FastAPI(title="Smart Study Room API", version="0.2.0")
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")


def now() -> datetime:
    return datetime.now(timezone.utc)


def stamp(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="minutes")


@contextmanager
def db():
    conn = sqlite3.connect(str(DB_PATH), timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
    finally:
        conn.close()


def init_db() -> None:
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS rooms (
          id INTEGER PRIMARY KEY, name TEXT NOT NULL, capacity INTEGER NOT NULL,
          amenities TEXT NOT NULL, hourly_rate INTEGER NOT NULL,
          online INTEGER NOT NULL DEFAULT 1, occupied INTEGER NOT NULL DEFAULT 0,
          light_on INTEGER NOT NULL DEFAULT 0, fan_on INTEGER NOT NULL DEFAULT 0,
          speaker_on INTEGER NOT NULL DEFAULT 0, last_seen TEXT
        );
        CREATE TABLE IF NOT EXISTS bookings (
          id TEXT PRIMARY KEY, room_id INTEGER NOT NULL REFERENCES rooms(id),
          user_id TEXT NOT NULL, start TEXT NOT NULL, end TEXT NOT NULL,
          people INTEGER NOT NULL, cost INTEGER NOT NULL, status TEXT NOT NULL,
          qr_exp TEXT NOT NULL, idempotency_key TEXT UNIQUE, created TEXT NOT NULL,
          amenities TEXT NOT NULL DEFAULT '[]'
        );
        CREATE TABLE IF NOT EXISTS events (
          id INTEGER PRIMARY KEY AUTOINCREMENT, booking_id TEXT, event TEXT NOT NULL,
          at TEXT NOT NULL, detail TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS audit (
          id INTEGER PRIMARY KEY AUTOINCREMENT, actor TEXT NOT NULL,
          action TEXT NOT NULL, detail TEXT NOT NULL, at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS devices (
          id TEXT PRIMARY KEY, room_id INTEGER NOT NULL REFERENCES rooms(id),
          device TEXT NOT NULL, name TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
          UNIQUE(room_id,device)
        );
        CREATE TABLE IF NOT EXISTS amenities (
          id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL COLLATE NOCASE UNIQUE,
          description TEXT NOT NULL DEFAULT '', enabled INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS room_amenities (
          room_id INTEGER NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
          amenity_id INTEGER NOT NULL REFERENCES amenities(id) ON DELETE CASCADE,
          PRIMARY KEY(room_id, amenity_id)
        );
        CREATE TABLE IF NOT EXISTS app_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)
        columns = {row[1] for row in c.execute("PRAGMA table_info(rooms)").fetchall()}
        if "speaker_on" not in columns:
            c.execute("ALTER TABLE rooms ADD COLUMN speaker_on INTEGER NOT NULL DEFAULT 0")
        if "last_seen" not in columns:
            c.execute("ALTER TABLE rooms ADD COLUMN last_seen TEXT")
        booking_columns = {row[1] for row in c.execute("PRAGMA table_info(bookings)").fetchall()}
        if "amenities" not in booking_columns:
            c.execute("ALTER TABLE bookings ADD COLUMN amenities TEXT NOT NULL DEFAULT '[]'")
        if c.execute("SELECT COUNT(*) FROM rooms").fetchone()[0] == 0:
            c.executemany("INSERT INTO rooms(id,name,capacity,amenities,hourly_rate) VALUES(?,?,?,?,?)", [
                (1, "Phòng học A101", 8, '[]', 40000),
                (2, "Phòng thảo luận B204", 6, '[]', 35000),
                (3, "Phòng lab C301", 20, '[]', 80000),
                (4, "Phòng nhóm D102", 4, '[]', 25000),
            ])
        if not c.execute("SELECT 1 FROM app_meta WHERE key='amenity_catalog_v2'").fetchone():
            c.execute("DELETE FROM room_amenities")
            c.execute("DELETE FROM amenities")
            amenity_ids = {}
            for key, (label, description) in ROOM_AMENITIES.items():
                cur = c.execute("INSERT INTO amenities(name,description,enabled) VALUES(?,?,1)", (label, description))
                amenity_ids[key] = cur.lastrowid
            for room in c.execute("SELECT id FROM rooms").fetchall():
                for amenity_id in amenity_ids.values():
                    c.execute("INSERT INTO room_amenities(room_id,amenity_id) VALUES(?,?)", (room["id"], amenity_id))
                c.execute("UPDATE rooms SET amenities='[]' WHERE id=?", (room["id"],))
            c.execute("INSERT INTO app_meta(key,value) VALUES('amenity_catalog_v2','done')")
        device_names = {"lamp": "Đèn", "fan": "Quạt", "speaker": "Loa"}
        for room in c.execute("SELECT id FROM rooms").fetchall():
            for device, label in device_names.items():
                c.execute("INSERT OR IGNORE INTO devices(id,room_id,device,name,enabled) VALUES(?,?,?,?,1)", ("%s_%s" % (room["id"], device), room["id"], device, label))


def init_firestore() -> None:
    global FIRESTORE
    credentials_json = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON", "").strip()
    credentials_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
    if not credentials_json and not credentials_path:
        return
    if firestore is None:
        raise RuntimeError("Thiếu google-cloud-firestore; hãy cài requirements.txt")
    if credentials_json:
        info = json.loads(credentials_json)
        credentials = service_account.Credentials.from_service_account_info(info)
        project_id = os.getenv("FIREBASE_PROJECT_ID", info.get("project_id", FIREBASE_PROJECT_ID))
        FIRESTORE = firestore.Client(project=project_id, credentials=credentials)
    else:
        FIRESTORE = firestore.Client(project=FIREBASE_PROJECT_ID)
    seed_firestore_rooms()
    migrate_firestore_amenities()
    seed_firestore_users_and_devices()


def seed_firestore_rooms() -> None:
    collection = FIRESTORE.collection("rooms")
    if any(collection.limit(1).stream()):
        return
    defaults = [
        (1, "Phòng học A101", 8, [], 40000),
        (2, "Phòng thảo luận B204", 6, [], 35000),
        (3, "Phòng lab C301", 20, [], 80000),
        (4, "Phòng nhóm D102", 4, [], 25000),
    ]
    migrated_rooms = []
    migrated_bookings = []
    migrated_audit = []
    migrated_events = []
    if DB_PATH.exists():
        try:
            with db() as c:
                migrated_rooms = [dict(row) for row in c.execute("SELECT * FROM rooms").fetchall()]
                migrated_bookings = [dict(row) for row in c.execute("SELECT * FROM bookings").fetchall()]
                migrated_audit = [dict(row) for row in c.execute("SELECT * FROM audit").fetchall()]
                migrated_events = [dict(row) for row in c.execute("SELECT * FROM events").fetchall()]
        except sqlite3.Error:
            migrated_rooms = []
    batch = FIRESTORE.batch()
    for raw in migrated_rooms:
        room = dict(raw)
        room["amenities"] = sqlite_room(raw).get("amenities", [])
        room.setdefault("speaker_on", 0)
        for key in ("online", "occupied", "light_on", "fan_on", "speaker_on"):
            room[key] = bool(room.get(key, key == "online"))
        batch.set(collection.document(str(room["id"])), room)
    if not migrated_rooms:
        for room_id, name, capacity, amenities, rate in defaults:
            batch.set(collection.document(str(room_id)), {
                "id": room_id, "name": name, "capacity": capacity, "amenities": amenities,
                "hourly_rate": rate, "online": True, "occupied": False,
                "light_on": False, "fan_on": False, "speaker_on": False,
            })
    batch.commit()
    for collection_name, rows in (("bookings", migrated_bookings), ("audit", migrated_audit), ("booking_events", migrated_events)):
        chunk_size = 200 if collection_name == "bookings" else 400
        for offset in range(0, len(rows), chunk_size):
            migration_batch = FIRESTORE.batch()
            for raw in rows[offset:offset + chunk_size]:
                item = dict(raw)
                item.pop("id", None)
                migration_batch.set(FIRESTORE.collection(collection_name).document(str(raw.get("id") or uuid.uuid4().hex)), item)
                if collection_name == "bookings" and raw.get("idempotency_key"):
                    idem_id = hashlib.sha256(raw["idempotency_key"].encode()).hexdigest()
                    migration_batch.set(FIRESTORE.collection("idempotency").document(idem_id), {"booking_id": str(raw["id"])})
            migration_batch.commit()


def seed_firestore_users_and_devices() -> None:
    users = FIRESTORE.collection("users")
    for user_id, username, role, name in (
        ("demo-user", "user", "user", "Người dùng demo"),
        ("demo-admin", "admin", "admin", "Quản trị viên"),
    ):
        ref = users.document(user_id)
        if not ref.get().exists:
            ref.set({"user_id": user_id, "username": username, "role": role, "name": name, "demo": True})
    device_labels = {"lamp": "Đèn", "fan": "Quạt", "speaker": "Loa"}
    batch = FIRESTORE.batch()
    count = 0
    for room in fs_rooms():
        for device, label in device_labels.items():
            ref = FIRESTORE.collection("devices").document("%s_%s" % (room["id"], device))
            snapshot = ref.get()
            payload = {
                "id": "%s_%s" % (room["id"], device), "room_id": room["id"],
                "device": device,
                "on": bool(room.get(DEVICE_COLUMNS[device], False)),
                "topic_command": "rooms/%s/devices/%s/command" % (room["id"], device),
                "topic_status": "rooms/%s/devices/%s/status" % (room["id"], device),
            }
            if not snapshot.exists:
                payload.update({"name": label, "enabled": True})
            batch.set(ref, payload, merge=True)
            count += 1
            if count == 450:
                batch.commit()
                batch = FIRESTORE.batch()
                count = 0
    if count:
        batch.commit()


def fs_room(room_id: int) -> Optional[dict[str, Any]]:
    snapshot = FIRESTORE.collection("rooms").document(str(room_id)).get()
    if not snapshot.exists:
        return None
    room = snapshot.to_dict()
    room.setdefault("id", room_id)
    room["amenity_ids"], room["amenities"] = fs_room_amenities(room_id)
    for key in ("online", "occupied", "light_on", "fan_on", "speaker_on"):
        room.setdefault(key, False if key != "online" else True)
    return room


def fs_rooms() -> list[dict[str, Any]]:
    rooms = [fs_room(int(doc.id)) for doc in FIRESTORE.collection("rooms").stream()]
    return sorted((room for room in rooms if room), key=lambda room: room["id"])


def amenity_key(name: str) -> str:
    import unicodedata
    value = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", value).strip("-") or hashlib.sha1(name.encode()).hexdigest()[:12]


def migrate_firestore_amenities() -> None:
    """Replace legacy room details with the three supported device amenities once."""
    marker = FIRESTORE.collection("app_meta").document("amenity_catalog_v2")
    if marker.get().exists:
        return
    existing = {doc.id: (doc.to_dict() or {}) for doc in FIRESTORE.collection("amenities").stream()}
    for doc in FIRESTORE.collection("room_amenities").stream():
        doc.reference.delete()
    for doc in FIRESTORE.collection("amenities").stream():
        doc.reference.delete()
    for key, (name, description) in ROOM_AMENITIES.items():
        previous = existing.get(key, {})
        FIRESTORE.collection("amenities").document(key).set({
            "id": key, "name": name, "description": description,
            "enabled": bool(previous.get("enabled", True)),
        })
    for room_doc in FIRESTORE.collection("rooms").stream():
        for key in ROOM_AMENITIES:
            FIRESTORE.collection("room_amenities").document("%s_%s" % (room_doc.id, key)).set({"room_id": int(room_doc.id), "amenity_id": key})
        room = room_doc.to_dict() or {}
        if "amenities" in room:
            room_doc.reference.update({"amenities": firestore.DELETE_FIELD})
    marker.set({"version": 2, "migrated_at": stamp(now())})


def fs_room_amenities(room_id: int) -> tuple[list[str], list[str]]:
    linked = [doc.to_dict() or {} for doc in FIRESTORE.collection("room_amenities").where("room_id", "==", room_id).stream()]
    catalog = {doc.id: (doc.to_dict() or {}) for doc in FIRESTORE.collection("amenities").stream()}
    items = [(str(row.get("amenity_id", "")), catalog.get(str(row.get("amenity_id", "")), {}).get("name")) for row in linked]
    items = [(aid, name) for aid, name in items if name]
    enabled_names = [catalog[aid]["name"] for aid, _ in items if catalog[aid].get("enabled", True)]
    return [aid for aid, _ in items], enabled_names


def resolve_booking_amenities(room_id: int, requested_ids: list[str]) -> list[str]:
    """Validate selected devices against the room and return their display names."""
    ids = list(dict.fromkeys(str(value) for value in requested_ids))
    if not ids:
        return []
    if FIRESTORE is not None:
        linked_ids, _ = fs_room_amenities(room_id)
        catalog = {str(item["id"]): item for item in get_amenities()}
        if any(aid not in linked_ids or aid not in catalog for aid in ids):
            raise HTTPException(400, "Có tiện ích không khả dụng cho phòng đã chọn")
        return [catalog[aid]["name"] for aid in ids]
    placeholders = ",".join("?" for _ in ids)
    with db() as c:
        rows = c.execute(
            "SELECT a.id,a.name FROM amenities a JOIN room_amenities ra ON ra.amenity_id=a.id "
            "WHERE ra.room_id=? AND a.enabled=1 AND a.id IN (" + placeholders + ")",
            (room_id, *(int(aid) for aid in ids)),
        ).fetchall()
    names = {str(row["id"]): row["name"] for row in rows}
    if any(aid not in names for aid in ids):
        raise HTTPException(400, "Có tiện ích không khả dụng cho phòng đã chọn")
    return [names[aid] for aid in ids]


def get_amenities(include_disabled: bool = False) -> list[dict[str, Any]]:
    if FIRESTORE is not None:
        items = [dict(doc.to_dict() or {}, id=doc.id) for doc in FIRESTORE.collection("amenities").stream()]
        return sorted((x for x in items if include_disabled or x.get("enabled", True)), key=lambda x: x.get("name", "").casefold())
    with db() as c:
        q = "SELECT id,name,description,enabled FROM amenities" + ("" if include_disabled else " WHERE enabled=1") + " ORDER BY name COLLATE NOCASE"
        return [dict(row) for row in c.execute(q).fetchall()]


def sqlite_room(row: Any) -> dict[str, Any]:
    room = dict(row)
    with db() as c:
        links = c.execute("SELECT a.id,a.name,a.enabled FROM amenities a JOIN room_amenities ra ON ra.amenity_id=a.id WHERE ra.room_id=? ORDER BY a.name COLLATE NOCASE", (room["id"],)).fetchall()
    room["amenity_ids"] = [str(x["id"]) for x in links]
    room["amenities"] = [x["name"] for x in links if x["enabled"]]
    return room


def resolve_amenity_filter(values: list[str]) -> list[str]:
    catalog = get_amenities(include_disabled=True)
    by_id = {str(item["id"]): item["name"] for item in catalog}
    by_name = {item["name"].casefold(): item["name"] for item in catalog}
    return [by_id[v] if v in by_id else by_name[v.casefold()] for v in values if v in by_id or v.casefold() in by_name]


def fs_add_audit(actor: str, action: str, detail: str) -> None:
    FIRESTORE.collection("audit").add({"actor": actor, "action": action, "detail": detail, "at": stamp(now())})


def fs_add_event(booking_id: str, event: str, detail: str) -> None:
    FIRESTORE.collection("booking_events").add({"booking_id": booking_id, "event": event, "detail": detail, "at": stamp(now())})


def fs_update_room(room_id: int, updates: dict[str, Any]) -> bool:
    ref = FIRESTORE.collection("rooms").document(str(room_id))
    if not ref.get().exists:
        return False
    ref.update(updates)
    device_updates = {device: updates[column] for device, column in DEVICE_COLUMNS.items() if column in updates}
    if device_updates:
        for device, value in device_updates.items():
            FIRESTORE.collection("devices").document("%s_%s" % (room_id, device)).set({"on": bool(value)}, merge=True)
    return True


def transition_firestore_booking(booking_id: str, expected: str, target: str) -> bool:
    booking_ref = FIRESTORE.collection("bookings").document(booking_id)
    row_result = {"changed": False}

    @firestore.transactional
    def transition(transaction):
        snapshot = fs_transaction_get_one(transaction, booking_ref)
        if snapshot is None or not snapshot.exists or snapshot.to_dict().get("status") != expected:
            return False
        booking = snapshot.to_dict()
        room_ref = FIRESTORE.collection("rooms").document(str(booking["room_id"]))
        room_snapshot = None
        if target == "COMPLETED":
            room_snapshot = fs_transaction_get_one(transaction, room_ref)
        transaction.update(booking_ref, {"status": target})
        if target == "COMPLETED" and room_snapshot is not None and room_snapshot.exists:
            transaction.update(room_ref, {"occupied": False})
        event_ref = FIRESTORE.collection("booking_events").document()
        transaction.set(event_ref, {"booking_id": booking_id, "event": target, "at": stamp(now()), "detail": "Tự động chuyển trạng thái theo thời gian booking"})
        audit_ref = FIRESTORE.collection("audit").document()
        transaction.set(audit_ref, {"actor": "booking-lifecycle", "action": target, "detail": booking_id, "at": stamp(now())})
        return True

    row_result["changed"] = transition(FIRESTORE.transaction())
    return row_result["changed"]


def process_booking_lifecycle() -> None:
    current = now()
    if FIRESTORE is not None:
        bookings = fs_bookings()
        for booking in bookings:
            status = booking.get("status")
            try:
                if status == "CONFIRMED" and current > parse_dt(booking["end"]) + timedelta(minutes=15):
                    transition_firestore_booking(booking["id"], "CONFIRMED", "NO_SHOW")
                elif status == "CHECKED_IN" and current >= parse_dt(booking["end"]):
                    if transition_firestore_booking(booking["id"], "CHECKED_IN", "COMPLETED"):
                        transport = publish_room_command(booking["room_id"], {"lamp": False, "fan": False, "speaker": False})
                        if transport["transport"] != "mqtt" or not transport["published"]:
                            fs_update_room(booking["room_id"], {"light_on": False, "fan_on": False, "speaker_on": False})
            except Exception as exc:
                print("Booking lifecycle error %s: %s" % (type(exc).__name__, str(exc)), flush=True)
        for room in fs_rooms():
            last_seen = room.get("last_seen")
            if last_seen and room.get("online"):
                try:
                    if current - parse_dt(last_seen) > timedelta(seconds=60):
                        fs_update_room(room["id"], {"online": False})
                        fs_add_audit("room-monitor", "CONTROLLER_OFFLINE", "room=%s; last_seen=%s" % (room["id"], last_seen))
                except Exception:
                    continue
        return

    with LOCK, db() as c:
        c.execute("BEGIN IMMEDIATE")
        no_show = c.execute("SELECT * FROM bookings WHERE status='CONFIRMED'").fetchall()
        checked_in = c.execute("SELECT * FROM bookings WHERE status='CHECKED_IN'").fetchall()
        for row in no_show:
            if current > parse_dt(row["end"]) + timedelta(minutes=15):
                changed = c.execute("UPDATE bookings SET status='NO_SHOW' WHERE id=? AND status='CONFIRMED'", (row["id"],)).rowcount
                if changed:
                    c.execute("INSERT INTO events(booking_id,event,at,detail) VALUES(?,?,?,?)", (row["id"], "NO_SHOW", stamp(current), "Booking quá hạn nhưng chưa check-in"))
                    audit(c, "booking-lifecycle", "NO_SHOW", row["id"])
        completed_ids = []
        for row in checked_in:
            if current >= parse_dt(row["end"]):
                changed = c.execute("UPDATE bookings SET status='COMPLETED' WHERE id=? AND status='CHECKED_IN'", (row["id"],)).rowcount
                if changed:
                    c.execute("UPDATE rooms SET occupied=0,light_on=0,fan_on=0,speaker_on=0 WHERE id=?", (row["room_id"],))
                    c.execute("INSERT INTO events(booking_id,event,at,detail) VALUES(?,?,?,?)", (row["id"], "COMPLETED", stamp(current), "Hết giờ; tự tắt thiết bị mô phỏng"))
                    audit(c, "booking-lifecycle", "COMPLETED", row["id"])
                    completed_ids.append((row["id"], row["room_id"]))
        room_rows = c.execute("SELECT id,online,last_seen FROM rooms WHERE online=1 AND last_seen IS NOT NULL").fetchall()
        for room in room_rows:
            try:
                if current - parse_dt(room["last_seen"]) > timedelta(seconds=60):
                    c.execute("UPDATE rooms SET online=0 WHERE id=? AND online=1", (room["id"],))
                    audit(c, "room-monitor", "CONTROLLER_OFFLINE", "room=%s; last_seen=%s" % (room["id"], room["last_seen"]))
            except Exception:
                continue
        c.execute("COMMIT")
    for _, room_id in completed_ids:
        publish_room_command(room_id, {"lamp": False, "fan": False, "speaker": False})


def lifecycle_worker() -> None:
    while not LIFECYCLE_STOP.wait(15):
        try:
            process_booking_lifecycle()
        except Exception as exc:
            print("Booking lifecycle worker error %s: %s" % (type(exc).__name__, str(exc)), flush=True)


def fs_bookings() -> list[dict[str, Any]]:
    result = []
    for doc in FIRESTORE.collection("bookings").stream():
        item = doc.to_dict()
        item.setdefault("id", doc.id)
        result.append(item)
    return result


def fs_transaction_get_one(transaction, reference):
    """Transaction.get yields snapshots in the Python Firestore client."""
    return next(iter(transaction.get(reference)), None)


def fs_booking_conflict(room_id: int, start: str, end: str, exclude_id: Optional[str] = None) -> bool:
    query = FIRESTORE.collection("bookings").where("room_id", "==", room_id)
    for snapshot in query.stream():
        booking = snapshot.to_dict()
        booking.setdefault("id", snapshot.id)
        if booking.get("id") == exclude_id or booking.get("room_id") != room_id:
            continue
        if booking.get("status") not in ("CONFIRMED", "CHECKED_IN"):
            continue
        if booking.get("start", "") < end and booking.get("end", "") > start:
            return True
    return False


def fs_create_booking(data: "BookingIn", identity: dict[str, str], start: datetime, end: datetime, amenities: list[str]) -> dict[str, Any]:
    booking_id = str(uuid.uuid4())
    booking_ref = FIRESTORE.collection("bookings").document(booking_id)
    room_ref = FIRESTORE.collection("rooms").document(str(data.room_id))
    idem_ref = None
    if data.idempotency_key:
        idem_id = hashlib.sha256(data.idempotency_key.encode()).hexdigest()
        idem_ref = FIRESTORE.collection("idempotency").document(idem_id)
    expiration = end + timedelta(minutes=15)
    booking = None

    @firestore.transactional
    def commit(transaction):
        nonlocal booking
        if idem_ref is not None:
            existing_idempotency = fs_transaction_get_one(transaction, idem_ref)
            if existing_idempotency is not None and existing_idempotency.exists:
                existing_data = existing_idempotency.to_dict() or {}
                existing_booking_id = existing_data.get("booking_id")
                if existing_booking_id:
                    existing_booking = fs_transaction_get_one(
                        transaction,
                        FIRESTORE.collection("bookings").document(existing_booking_id),
                    )
                    if existing_booking is not None and existing_booking.exists:
                        booking = {**existing_booking.to_dict(), "id": existing_booking.id}
                        return booking
        room_snapshot = fs_transaction_get_one(transaction, room_ref)
        if room_snapshot is None or not room_snapshot.exists:
            raise HTTPException(404, "Không tìm thấy phòng")
        room = room_snapshot.to_dict()
        if data.people > int(room.get("capacity", 0)):
            raise HTTPException(400, "Số người vượt sức chứa phòng")
        room_bookings = transaction.get(FIRESTORE.collection("bookings").where("room_id", "==", data.room_id))
        for snapshot in room_bookings:
            other = snapshot.to_dict()
            if other.get("status") in ("CONFIRMED", "CHECKED_IN") and other.get("start", "") < stamp(end) and other.get("end", "") > stamp(start):
                raise HTTPException(409, "Phòng vừa được đặt trong khoảng thời gian này")
        cost = round(int(room.get("hourly_rate", 0)) * (end - start).total_seconds() / 3600)
        booking = {
            "id": booking_id, "room_id": data.room_id, "user_id": identity["user_id"],
            "start": stamp(start), "end": stamp(end), "people": data.people,
            "cost": cost, "status": "CONFIRMED", "qr_exp": stamp(expiration), "amenities": amenities,
            "idempotency_key": data.idempotency_key, "created": stamp(now()),
        }
        transaction.create(booking_ref, booking)
        transaction.update(room_ref, {"booking_version": int(room.get("booking_version", 0)) + 1})
        event_ref = FIRESTORE.collection("booking_events").document()
        transaction.set(event_ref, {"booking_id": booking_id, "event": "PENDING", "at": stamp(now()), "detail": "Yêu cầu đã qua bước xác nhận của người dùng"})
        confirmed_event_ref = FIRESTORE.collection("booking_events").document()
        transaction.set(confirmed_event_ref, {"booking_id": booking_id, "event": "CONFIRMED", "at": stamp(now()), "detail": "Booking đã xác nhận"})
        audit_ref = FIRESTORE.collection("audit").document()
        transaction.set(audit_ref, {"actor": identity["user_id"], "action": "BOOKING_CREATED", "detail": booking_id, "at": stamp(now())})
        if idem_ref is not None:
            transaction.set(idem_ref, {"booking_id": booking_id})
        return booking

    try:
        return booking_view(commit(FIRESTORE.transaction()))
    except HTTPException:
        raise
    except Exception as exc:
        import traceback

        print("========== FIRESTORE BOOKING ERROR ==========")
        print("ERROR TYPE:", type(exc).__name__)
        print("ERROR:", repr(exc))
        traceback.print_exc()
        print("==============================================", flush=True)

        raise HTTPException(
            503,
            f"Firestore error: {type(exc).__name__}: {str(exc)}"
        ) from exc

@app.on_event("startup")
def startup() -> None:
    global LIFECYCLE_THREAD
    init_db()
    init_firestore()
    start_mqtt()
    LIFECYCLE_STOP.clear()
    if LIFECYCLE_THREAD is None or not LIFECYCLE_THREAD.is_alive():
        LIFECYCLE_THREAD = threading.Thread(target=lifecycle_worker, name="booking-lifecycle", daemon=True)
        LIFECYCLE_THREAD.start()


@app.on_event("shutdown")
def shutdown() -> None:
    LIFECYCLE_STOP.set()
    if MQTT_CLIENT is not None:
        MQTT_CLIENT.loop_stop()


DEVICE_COLUMNS = {"lamp": "light_on", "fan": "fan_on", "speaker": "speaker_on"}


def mqtt_on_connect(client, userdata, flags, reason_code, properties=None):
    global MQTT_CONNECTED
    MQTT_CONNECTED = int(reason_code) == 0
    if MQTT_CONNECTED:
        client.subscribe("rooms/+/telemetry", qos=1)
        client.subscribe("rooms/+/status", qos=1)
        client.subscribe("rooms/+/devices/+/status", qos=1)


def mqtt_on_disconnect(client, userdata, disconnect_flags, reason_code, properties=None):
    global MQTT_CONNECTED
    MQTT_CONNECTED = False


def apply_device_message(room_id: int, payload: dict[str, Any], actor: str = "mqtt-controller") -> None:
    assignments = []
    values = []
    if "online" in payload:
        assignments.append("online=?")
        values.append(int(bool(payload["online"])))
    elif actor == "mqtt-controller":
        assignments.append("online=?")
        values.append(1)
    if "occupied" in payload:
        assignments.append("occupied=?")
        values.append(int(bool(payload["occupied"])))
    devices = payload.get("devices", {})
    if not isinstance(devices, dict):
        devices = {}
    if "device" in payload and "on" in payload:
        devices = dict(devices, **{payload["device"]: payload["on"]})
    for device, state in devices.items():
        column = DEVICE_COLUMNS.get(device)
        if column and isinstance(state, bool):
            assignments.append(column + "=?")
            values.append(int(state))
    if actor == "mqtt-controller":
        assignments.append("last_seen=?")
        values.append(stamp(now()))
    if not assignments:
        return
    if FIRESTORE is not None:
        field_updates = {}
        for assignment, value in zip(assignments, values):
            field = assignment.split("=")[0]
            field_updates[field] = bool(value) if field in ("online", "occupied", "light_on", "fan_on", "speaker_on") else value
        fs_update_room(room_id, field_updates)
        fs_add_audit(actor, "MQTT_STATUS", "room=%s; %s" % (room_id, json.dumps(payload, ensure_ascii=False)))
        return
    with db() as c:
        values.append(room_id)
        c.execute("UPDATE rooms SET " + ",".join(assignments) + " WHERE id=?", values)
        audit(c, actor, "MQTT_STATUS", "room=%s; %s" % (room_id, json.dumps(payload, ensure_ascii=False)))


def mqtt_on_message(client, userdata, message):
    try:
        parts = message.topic.split("/")
        room_id = int(parts[1])
        payload = json.loads(message.payload.decode("utf-8"))
        if len(parts) == 5 and parts[2] == "devices" and parts[4] == "status":
            payload = dict(payload, device=parts[3])
        apply_device_message(room_id, payload)
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError, TypeError):
        return
    except Exception:
        return


def start_mqtt() -> None:
    global MQTT_CLIENT
    broker = os.getenv("MQTT_BROKER", "").strip()
    if not broker or mqtt is None:
        return
    try:
        port = int(os.getenv("MQTT_PORT", "8883" if os.getenv("MQTT_TLS", "true").lower() == "true" else "1883"))
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=os.getenv("MQTT_CLIENT_ID", "smart-room-backend"))
        username = os.getenv("MQTT_USERNAME", "")
        if username:
            client.username_pw_set(username, os.getenv("MQTT_PASSWORD", ""))
        if os.getenv("MQTT_TLS", "true").lower() == "true":
            client.tls_set()
        client.on_connect = mqtt_on_connect
        client.on_disconnect = mqtt_on_disconnect
        client.on_message = mqtt_on_message
        client.connect_async(broker, port, keepalive=30)
        client.loop_start()
        MQTT_CLIENT = client
    except Exception:
        MQTT_CLIENT = None


def publish_room_command(room_id: int, devices: dict[str, bool]) -> dict[str, Any]:
    payload = {"command_id": uuid.uuid4().hex, "devices": devices, "at": stamp(now())}
    topic = "rooms/%s/command" % room_id
    if MQTT_CLIENT is not None and MQTT_CONNECTED:
        result = MQTT_CLIENT.publish(topic, json.dumps(payload), qos=1, retain=False)
        return {"transport": "mqtt", "topic": topic, "published": result.rc == mqtt.MQTT_ERR_SUCCESS}
    return {"transport": "http-simulation", "topic": topic, "published": False}


def audit(c: sqlite3.Connection, actor: str, action: str, detail: str) -> None:
    c.execute("INSERT INTO audit(actor,action,detail,at) VALUES(?,?,?,?)", (actor, action, detail, stamp(now())))


def require_admin(role: Optional[str]) -> None:
    if role != "admin":
        raise HTTPException(403, "Yêu cầu quyền Administrator")


class BookingIn(BaseModel):
    room_id: int
    start: datetime
    end: datetime
    people: int = Field(ge=1, le=100)
    amenity_ids: list[str] = Field(default_factory=list)
    user_id: str = "demo-user"
    idempotency_key: Optional[str] = None


class ChatIn(BaseModel):
    message: str
    session_id: str = "default"


class QRIn(BaseModel):
    token: str
    room_id: int


class TelemetryIn(BaseModel):
    room_id: int
    occupied: bool
    online: bool = True
    devices: Optional[dict[str, bool]] = None


class DeviceCommandIn(BaseModel):
    devices: dict[str, bool]


class DeviceConfigIn(BaseModel):
    name: Optional[str] = None
    enabled: Optional[bool] = None


class LoginIn(BaseModel):
    username: str
    password: str


DEMO_ACCOUNTS = {
    "user": {"password": "demo123", "user_id": "demo-user", "role": "user", "name": "Người dùng demo"},
    "admin": {"password": "admin123", "user_id": "demo-admin", "role": "admin", "name": "Quản trị viên"},
}


def current_user(authorization: Optional[str] = Header(default=None)) -> dict[str, str]:
    scheme, _, token = (authorization or "").partition(" ")
    identity = SESSIONS.get(token) if scheme.lower() == "bearer" else None
    if not identity:
        raise HTTPException(401, "Vui lòng đăng nhập")
    return identity


def parse_dt(raw: str) -> datetime:
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    except ValueError as exc:
        raise HTTPException(400, "Thời gian phải theo ISO 8601") from exc


def free_rooms(start: datetime, end: datetime, people: int, amenities: Optional[list[str]] = None) -> list[dict[str, Any]]:
    if FIRESTORE is not None:
        requested = {a.casefold() for a in (amenities or [])}
        result = []
        for room in fs_rooms():
            if int(room.get("capacity", 0)) < people:
                continue
            current_amenities = room.get("amenities", [])
            if requested and not requested.issubset({a.casefold() for a in current_amenities}):
                continue
            if not fs_booking_conflict(room["id"], stamp(start), stamp(end)):
                result.append(room)
        return sorted(result, key=lambda room: room.get("hourly_rate", 0))
    with db() as c:
        rows = c.execute("SELECT * FROM rooms WHERE capacity>=? ORDER BY hourly_rate", (people,)).fetchall()
        result = []
        for row in rows:
            room = sqlite_room(row)
            if amenities and not {a.casefold() for a in amenities}.issubset({a.casefold() for a in room["amenities"]}):
                continue
            conflict = c.execute("""SELECT 1 FROM bookings WHERE room_id=?
              AND status IN ('CONFIRMED','CHECKED_IN') AND start<? AND end>? LIMIT 1""",
              (room["id"], stamp(end), stamp(start))).fetchone()
            if not conflict:
                result.append(room)
        return result


def sign_payload(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    body = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    sig = hmac.new(SECRET, body.encode(), hashlib.sha256).hexdigest()
    return body + "." + sig


def decode_token(token: str) -> dict[str, Any]:
    try:
        body, signature = token.split(".", 1)
        expected = hmac.new(SECRET, body.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError("signature")
        return json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    except Exception as exc:
        raise HTTPException(400, "QR không hợp lệ hoặc đã bị thay đổi") from exc


def booking_view(row: Union[sqlite3.Row, dict]) -> dict[str, Any]:
    booking = dict(row)
    if isinstance(booking.get("amenities"), str):
        try:
            booking["amenities"] = json.loads(booking["amenities"])
        except (TypeError, json.JSONDecodeError):
            booking["amenities"] = []
    if not isinstance(booking.get("amenities"), list):
        booking["amenities"] = []
    else:
        booking["amenities"] = [str(item) for item in booking["amenities"] if isinstance(item, (str, int, float))]
    booking["qr_token"] = sign_payload({"booking_id": booking["id"], "room_id": booking["room_id"], "exp": booking["qr_exp"]})
    return booking


@app.get("/")
def home():
    return FileResponse(str(BASE / "static" / "index.html"))


@app.get("/api/health")
def health():
    ai_status = {"ai_provider": "groq", "ai_enabled": bool(os.getenv("GROQ_API_KEY", "").strip()), "ai_model": GROQ_MODEL}
    if FIRESTORE is None:
        return {"status": "ok", "database": "sqlite", "firestore_connected": False, **ai_status}
    try:
        next(iter(FIRESTORE.collection("rooms").limit(1).stream()), None)
        return {"status": "ok", "database": "firestore", "firestore_connected": True, **ai_status}
    except Exception as exc:
        print("Firestore health check failed: %s: %s" % (type(exc).__name__, str(exc)), flush=True)
        return {"status": "degraded", "database": "firestore", "firestore_connected": False, "error": type(exc).__name__, **ai_status}


@app.post("/api/auth/login")
def login(data: LoginIn):
    account = DEMO_ACCOUNTS.get(data.username)
    if not account or not hmac.compare_digest(data.password, account["password"]):
        raise HTTPException(401, "Tên đăng nhập hoặc mật khẩu không đúng")
    token = secrets.token_urlsafe(32)
    SESSIONS[token] = {"user_id": account["user_id"], "role": account["role"], "name": account["name"]}
    return {"token": token, "user": SESSIONS[token]}


@app.get("/api/auth/me")
def whoami(identity: dict[str, str] = Depends(current_user)):
    return identity


@app.get("/api/rooms")
def rooms(identity: dict[str, str] = Depends(current_user)):
    if FIRESTORE is not None:
        return [{**r, "devices": {"lamp": bool(r["light_on"]), "fan": bool(r["fan_on"]), "speaker": bool(r["speaker_on"])}} for r in fs_rooms()]
    with db() as c:
        return [{**sqlite_room(r), "devices": {"lamp": bool(r["light_on"]), "fan": bool(r["fan_on"]), "speaker": bool(r["speaker_on"])}} for r in c.execute("SELECT * FROM rooms ORDER BY id").fetchall()]


@app.get("/api/availability")
def availability(start: str, end: str, people: int = 1, amenities: str = "", identity: dict[str, str] = Depends(current_user)):
    s, e = parse_dt(start), parse_dt(end)
    if s >= e or s < now() - timedelta(minutes=1):
        raise HTTPException(400, "Khoảng thời gian không hợp lệ")
    return free_rooms(s, e, people, resolve_amenity_filter([x.strip() for x in amenities.split(",") if x.strip()]))


@app.get("/api/amenities")
def amenities(identity: dict[str, str] = Depends(current_user)):
    return get_amenities()


@app.get("/api/admin/amenities")
def admin_amenities(identity: dict[str, str] = Depends(current_user)):
    require_admin(identity["role"])
    return get_amenities(include_disabled=True)


@app.post("/api/admin/amenities")
def create_amenity(values: dict[str, Any], identity: dict[str, str] = Depends(current_user)):
    require_admin(identity["role"])
    raise HTTPException(400, "Danh mục tiện ích cố định gồm Đèn, Quạt và Loa")


@app.patch("/api/admin/amenities/{amenity_id}")
def update_amenity(amenity_id: str, values: dict[str, Any], identity: dict[str, str] = Depends(current_user)):
    require_admin(identity["role"])
    if not values or set(values) - {"enabled"}:
        raise HTTPException(400, "Chỉ có thể bật hoặc tắt tiện ích Đèn, Quạt, Loa")
    if "enabled" in values:
        values["enabled"] = bool(values["enabled"])
    try:
        if FIRESTORE is not None:
            ref = FIRESTORE.collection("amenities").document(amenity_id)
            if not ref.get().exists:
                raise HTTPException(404, "Không tìm thấy tiện ích")
            ref.update(values)
            fs_add_audit("admin", "AMENITY_UPDATED", amenity_id + ": " + json.dumps(values, ensure_ascii=False))
        else:
            with db() as c:
                result = c.execute("UPDATE amenities SET " + ",".join(k+"=?" for k in values) + " WHERE id=?", (*values.values(), int(amenity_id)))
                if not result.rowcount:
                    raise HTTPException(404, "Không tìm thấy tiện ích")
                audit(c, "admin", "AMENITY_UPDATED", amenity_id + ": " + json.dumps(values, ensure_ascii=False))
    except sqlite3.IntegrityError as exc:
        raise HTTPException(409, "Tên tiện ích đã được sử dụng") from exc
    return {"ok": True, "id": amenity_id, **values}


@app.put("/api/admin/rooms/{room_id}/amenities")
def assign_room_amenities(room_id: int, values: dict[str, Any], identity: dict[str, str] = Depends(current_user)):
    require_admin(identity["role"])
    ids = [str(x) for x in values.get("amenity_ids", [])]
    catalog = {str(item["id"]): item for item in get_amenities(include_disabled=True)}
    if any(aid not in catalog for aid in ids):
        raise HTTPException(400, "Có tiện ích không tồn tại")
    if FIRESTORE is not None:
        if not FIRESTORE.collection("rooms").document(str(room_id)).get().exists:
            raise HTTPException(404, "Không tìm thấy phòng")
        collection = FIRESTORE.collection("room_amenities")
        batch = FIRESTORE.batch()
        for doc in collection.where("room_id", "==", room_id).stream():
            batch.delete(doc.reference)
        for aid in ids:
            batch.set(collection.document("%s_%s" % (room_id, aid)), {"room_id": room_id, "amenity_id": aid})
        batch.commit()
        fs_add_audit("admin", "ROOM_AMENITIES_UPDATED", "room=%s; amenities=%s" % (room_id, ",".join(ids)))
    else:
        with db() as c:
            if not c.execute("SELECT 1 FROM rooms WHERE id=?", (room_id,)).fetchone():
                raise HTTPException(404, "Không tìm thấy phòng")
            c.execute("DELETE FROM room_amenities WHERE room_id=?", (room_id,))
            for aid in ids:
                c.execute("INSERT INTO room_amenities(room_id,amenity_id) VALUES(?,?)", (room_id, int(aid)))
            audit(c, "admin", "ROOM_AMENITIES_UPDATED", "room=%s; amenities=%s" % (room_id, ",".join(ids)))
    return {"ok": True, "room_id": room_id, "amenity_ids": ids}


@app.post("/api/bookings")
def create_booking(data: BookingIn, identity: dict[str, str] = Depends(current_user)):
    start = data.start.replace(tzinfo=timezone.utc) if data.start.tzinfo is None else data.start.astimezone(timezone.utc)
    end = data.end.replace(tzinfo=timezone.utc) if data.end.tzinfo is None else data.end.astimezone(timezone.utc)
    if start >= end or start < now() - timedelta(minutes=1):
        raise HTTPException(400, "Khoảng thời gian không hợp lệ")
    amenities = resolve_booking_amenities(data.room_id, data.amenity_ids)
    if FIRESTORE is not None:
        return fs_create_booking(data, identity, start, end, amenities)
    with LOCK, db() as c:
        c.execute("BEGIN IMMEDIATE")
        try:
            if data.idempotency_key:
                existing = c.execute("SELECT * FROM bookings WHERE idempotency_key=?", (data.idempotency_key,)).fetchone()
                if existing:
                    c.execute("COMMIT")
                    return booking_view(existing)
            room = c.execute("SELECT * FROM rooms WHERE id=?", (data.room_id,)).fetchone()
            if not room:
                raise HTTPException(404, "Không tìm thấy phòng")
            if data.people > room["capacity"]:
                raise HTTPException(400, "Số người vượt sức chứa phòng")
            conflict = c.execute("""SELECT 1 FROM bookings WHERE room_id=?
              AND status IN ('CONFIRMED','CHECKED_IN') AND start<? AND end>? LIMIT 1""",
              (data.room_id, stamp(end), stamp(start))).fetchone()
            if conflict:
                raise HTTPException(409, "Phòng vừa được đặt trong khoảng thời gian này")
            booking_id = str(uuid.uuid4())
            cost = round(room["hourly_rate"] * (end - start).total_seconds() / 3600)
            expiration = end + timedelta(minutes=15)
            c.execute("INSERT INTO bookings(id,room_id,user_id,start,end,people,cost,status,qr_exp,idempotency_key,created,amenities) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (booking_id, data.room_id, identity["user_id"], stamp(start), stamp(end), data.people, cost, "CONFIRMED", stamp(expiration), data.idempotency_key, stamp(now()), json.dumps(amenities, ensure_ascii=False)))
            c.execute("INSERT INTO events(booking_id,event,at,detail) VALUES(?,?,?,?)", (booking_id, "PENDING", stamp(now()), "Yêu cầu đã qua bước xác nhận của người dùng"))
            c.execute("INSERT INTO events(booking_id,event,at,detail) VALUES(?,?,?,?)", (booking_id, "CONFIRMED", stamp(now()), "Booking đã xác nhận"))
            audit(c, identity["user_id"], "BOOKING_CREATED", booking_id)
            c.execute("COMMIT")
            return booking_view(c.execute("SELECT * FROM bookings WHERE id=?", (booking_id,)).fetchone())
        except Exception:
            c.execute("ROLLBACK")
            raise


@app.get("/api/bookings")
def list_bookings(identity: dict[str, str] = Depends(current_user)):
    if FIRESTORE is not None:
        items = [b for b in fs_bookings() if b.get("user_id") == identity["user_id"]]
        return [booking_view(b) for b in sorted(items, key=lambda b: b.get("created", ""), reverse=True)]
    with db() as c:
        return [booking_view(r) for r in c.execute("SELECT * FROM bookings WHERE user_id=? ORDER BY created DESC", (identity["user_id"],)).fetchall()]


@app.get("/api/bookings/{booking_id}")
def get_booking(booking_id: str, identity: dict[str, str] = Depends(current_user)):
    if FIRESTORE is not None:
        snapshot = FIRESTORE.collection("bookings").document(booking_id).get()
        if not snapshot.exists:
            raise HTTPException(404, "Không tìm thấy booking")
        row = {**snapshot.to_dict(), "id": snapshot.id}
    else:
        with db() as c:
            snapshot = c.execute("SELECT * FROM bookings WHERE id=?", (booking_id,)).fetchone()
            if not snapshot:
                raise HTTPException(404, "Không tìm thấy booking")
            row = dict(snapshot)
    if identity["role"] != "admin" and row.get("user_id") != identity["user_id"]:
        raise HTTPException(403, "Không có quyền xem booking này")
    return booking_view(row)


@app.get("/api/bookings/{booking_id}/events")
def booking_events(booking_id: str, identity: dict[str, str] = Depends(current_user)):
    booking = get_booking(booking_id, identity)
    if FIRESTORE is not None:
        events = [doc.to_dict() for doc in FIRESTORE.collection("booking_events").where("booking_id", "==", booking_id).stream()]
    else:
        with db() as c:
            events = [dict(row) for row in c.execute("SELECT * FROM events WHERE booking_id=? ORDER BY id", (booking_id,)).fetchall()]
    return {"booking_id": booking["id"], "events": sorted(events, key=lambda event: event.get("at", ""))}


@app.post("/api/bookings/{booking_id}/cancel")
def cancel_booking(booking_id: str, identity: dict[str, str] = Depends(current_user)):
    if FIRESTORE is not None:
        ref = FIRESTORE.collection("bookings").document(booking_id)
        @firestore.transactional
        def cancel(transaction):
            snapshot = fs_transaction_get_one(transaction, ref)
            if snapshot is None or not snapshot.exists:
                raise HTTPException(404, "Không tìm thấy booking")
            row = snapshot.to_dict()
            if identity["role"] != "admin" and row.get("user_id") != identity["user_id"]:
                raise HTTPException(403, "Không có quyền hủy booking này")
            if row.get("status") != "CONFIRMED":
                raise HTTPException(409, "Chỉ hủy được booking CONFIRMED")
            transaction.update(ref, {"status": "CANCELLED"})
            event_ref = FIRESTORE.collection("booking_events").document()
            audit_ref = FIRESTORE.collection("audit").document()
            transaction.set(event_ref, {"booking_id": booking_id, "event": "CANCELLED", "detail": "Người dùng hủy booking", "at": stamp(now())})
            transaction.set(audit_ref, {"actor": identity["user_id"], "action": "BOOKING_CANCELLED", "detail": booking_id, "at": stamp(now())})
            return True
        cancel(FIRESTORE.transaction())
        return {"ok": True, "status": "CANCELLED"}
    with LOCK, db() as c:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute("SELECT * FROM bookings WHERE id=?", (booking_id,)).fetchone()
        if not row:
            c.execute("ROLLBACK")
            raise HTTPException(404, "Không tìm thấy booking")
        if identity["role"] != "admin" and row["user_id"] != identity["user_id"]:
            c.execute("ROLLBACK")
            raise HTTPException(403, "Không có quyền hủy booking này")
        if row["status"] != "CONFIRMED":
            c.execute("ROLLBACK")
            raise HTTPException(409, "Chỉ hủy được booking CONFIRMED")
        c.execute("UPDATE bookings SET status='CANCELLED' WHERE id=? AND status='CONFIRMED'", (booking_id,))
        c.execute("INSERT INTO events(booking_id,event,at,detail) VALUES(?,?,?,?)", (booking_id, "CANCELLED", stamp(now()), "Người dùng hủy booking"))
        audit(c, identity["user_id"], "BOOKING_CANCELLED", booking_id)
        c.execute("COMMIT")
        return {"ok": True, "status": "CANCELLED"}


@app.post("/api/qr/validate")
def validate_qr(data: QRIn, identity: dict[str, str] = Depends(current_user)):
    payload = decode_token(data.token)
    if payload.get("room_id") != data.room_id:
        raise HTTPException(403, "QR không thuộc phòng này")
    if now() > parse_dt(payload.get("exp", "")):
        raise HTTPException(403, "QR đã hết hạn")
    if FIRESTORE is not None:
        booking_ref = FIRESTORE.collection("bookings").document(payload.get("booking_id", ""))
        room_ref = FIRESTORE.collection("rooms").document(str(data.room_id))

        @firestore.transactional
        def checkin(transaction):
            booking_snapshot = fs_transaction_get_one(transaction, booking_ref)
            if booking_snapshot is None or not booking_snapshot.exists:
                raise HTTPException(404, "Không tìm thấy booking phù hợp")
            row = booking_snapshot.to_dict()
            if row.get("room_id") != data.room_id:
                raise HTTPException(404, "Không tìm thấy booking phù hợp")
            if identity["role"] != "admin" and row.get("user_id") != identity["user_id"]:
                raise HTTPException(403, "QR không thuộc booking của tài khoản này")
            if row.get("status") != "CONFIRMED":
                raise HTTPException(409, "Booking không còn ở trạng thái CONFIRMED")
            if now() < parse_dt(row["start"]) - timedelta(minutes=15):
                raise HTTPException(403, "Check-in quá sớm")
            if now() > parse_dt(row["end"]) + timedelta(minutes=15):
                raise HTTPException(403, "Đã quá thời gian check-in")
            room_snapshot = fs_transaction_get_one(transaction, room_ref)
            if room_snapshot is None or not room_snapshot.exists:
                raise HTTPException(404, "Không tìm thấy phòng")
            transaction.update(booking_ref, {"status": "CHECKED_IN"})
            transaction.update(room_ref, {"occupied": True})
            event_ref = FIRESTORE.collection("booking_events").document()
            transaction.set(event_ref, {"booking_id": row["id"], "event": "CHECKED_IN", "at": stamp(now()), "detail": "QR hợp lệ; gửi lệnh bật đèn, quạt, loa"})
            audit_ref = FIRESTORE.collection("audit").document()
            transaction.set(audit_ref, {"actor": row["user_id"], "action": "CHECK_IN", "detail": row["id"], "at": stamp(now())})
            return row

        row = checkin(FIRESTORE.transaction())
        transport = publish_room_command(data.room_id, {"lamp": True, "fan": True, "speaker": True})
        if transport["transport"] != "mqtt" or not transport["published"]:
            fs_update_room(data.room_id, {"light_on": True, "fan_on": True, "speaker_on": True})
        return {"ok": True, "booking_id": row["id"], "status": "CHECKED_IN", "devices": {"lamp": True, "fan": True, "speaker": True}, **transport}
    with db() as c:
        row = c.execute("SELECT * FROM bookings WHERE id=?", (payload.get("booking_id"),)).fetchone()
        if not row or row["room_id"] != data.room_id:
            raise HTTPException(404, "Không tìm thấy booking phù hợp")
        if identity["role"] != "admin" and row["user_id"] != identity["user_id"]:
            raise HTTPException(403, "QR không thuộc booking của tài khoản này")
        if row["status"] != "CONFIRMED":
            raise HTTPException(409, "Booking không còn ở trạng thái CONFIRMED")
        if now() < parse_dt(row["start"]) - timedelta(minutes=15):
            raise HTTPException(403, "Check-in quá sớm")
        if now() > parse_dt(row["end"]) + timedelta(minutes=15):
            raise HTTPException(403, "Đã quá thời gian check-in")
        c.execute("BEGIN IMMEDIATE")
        c.execute("UPDATE bookings SET status='CHECKED_IN' WHERE id=? AND status='CONFIRMED'", (row["id"],))
        if c.execute("SELECT changes()").fetchone()[0] == 0:
            c.execute("ROLLBACK")
            raise HTTPException(409, "Booking đã được xử lý")
        c.execute("UPDATE rooms SET occupied=1 WHERE id=?", (data.room_id,))
        c.execute("INSERT INTO events(booking_id,event,at,detail) VALUES(?,?,?,?)", (row["id"], "CHECKED_IN", stamp(now()), "QR hợp lệ; bật thiết bị mô phỏng"))
        audit(c, row["user_id"], "CHECK_IN", row["id"])
        c.execute("COMMIT")
    transport = publish_room_command(data.room_id, {"lamp": True, "fan": True, "speaker": True})
    if transport["transport"] != "mqtt" or not transport["published"]:
        with db() as c:
            c.execute("UPDATE rooms SET light_on=1,fan_on=1,speaker_on=1 WHERE id=?", (data.room_id,))
    return {"ok": True, "booking_id": row["id"], "status": "CHECKED_IN", "devices": {"lamp": True, "fan": True, "speaker": True}, **transport}


@app.get("/api/qr/image")
def qr_image(token: str):
    import qrcode
    image = qrcode.make(token)
    stream = io.BytesIO()
    image.save(stream, format="PNG")
    stream.seek(0)
    return StreamingResponse(stream, media_type="image/png")


@app.post("/api/iot/telemetry")
def telemetry(data: TelemetryIn):
    if FIRESTORE is not None:
        room = fs_room(data.room_id)
        if not room:
            raise HTTPException(404, "Không tìm thấy phòng")
        devices = data.devices or {"lamp": data.occupied, "fan": data.occupied, "speaker": data.occupied}
        updates = {"online": data.online, "occupied": data.occupied}
        updates["last_seen"] = stamp(now())
        for device, state in devices.items():
            column = DEVICE_COLUMNS.get(device)
            if not column:
                raise HTTPException(400, "Thiết bị hợp lệ: lamp, fan, speaker")
            updates[column] = state
        fs_update_room(data.room_id, updates)
        fs_add_audit("http-controller", "TELEMETRY", "room=%s; occupied=%s; online=%s; devices=%s" % (data.room_id, data.occupied, data.online, json.dumps(devices)))
        if not data.occupied:
            for booking in fs_bookings():
                if booking.get("room_id") == data.room_id and booking.get("status") == "CHECKED_IN" and booking.get("end", "") <= stamp(now()):
                    FIRESTORE.collection("bookings").document(booking["id"]).update({"status": "COMPLETED"})
        return {"ok": True, "room_id": data.room_id, "online": data.online, "occupied": data.occupied, "devices": devices, "transport": "http-simulation"}
    with db() as c:
        room = c.execute("SELECT id FROM rooms WHERE id=?", (data.room_id,)).fetchone()
        if not room:
            raise HTTPException(404, "Không tìm thấy phòng")
        devices = data.devices or {"lamp": data.occupied, "fan": data.occupied, "speaker": data.occupied}
        assignments = ["online=?", "occupied=?", "last_seen=?"]
        values = [int(data.online), int(data.occupied), stamp(now())]
        for device, state in devices.items():
            column = DEVICE_COLUMNS.get(device)
            if not column:
                raise HTTPException(400, "Thiết bị hợp lệ: lamp, fan, speaker")
            assignments.append(column + "=?")
            values.append(int(state))
        values.append(data.room_id)
        c.execute("UPDATE rooms SET " + ",".join(assignments) + " WHERE id=?", values)
        c.execute("INSERT INTO audit(actor,action,detail,at) VALUES(?,?,?,?)", ("http-controller", "TELEMETRY", f"room={data.room_id}; occupied={data.occupied}; online={data.online}; devices={json.dumps(devices)}", stamp(now())))
        if not data.occupied:
            c.execute("UPDATE bookings SET status='COMPLETED' WHERE room_id=? AND status='CHECKED_IN' AND end<=?", (data.room_id, stamp(now())))
        return {"ok": True, "room_id": data.room_id, "online": data.online, "occupied": data.occupied, "devices": devices, "transport": "http-simulation"}


@app.post("/api/admin/rooms/{room_id}/devices")
def command_devices(room_id: int, data: DeviceCommandIn, identity: dict[str, str] = Depends(current_user)):
    require_admin(identity["role"])
    if not data.devices or set(data.devices) - set(DEVICE_COLUMNS):
        raise HTTPException(400, "Thiết bị hợp lệ: lamp, fan, speaker")
    if FIRESTORE is not None:
        room = fs_room(room_id)
        if not room:
            raise HTTPException(404, "Không tìm thấy phòng")
        for device in data.devices:
            config = FIRESTORE.collection("devices").document("%s_%s" % (room_id, device)).get()
            if config.exists and not config.to_dict().get("enabled", True):
                raise HTTPException(409, "Thiết bị %s đang bị Admin vô hiệu hóa" % device)
    else:
        with db() as c:
            if not c.execute("SELECT id FROM rooms WHERE id=?", (room_id,)).fetchone():
                raise HTTPException(404, "Không tìm thấy phòng")
            placeholders = ",".join("?" for _ in data.devices)
            disabled = c.execute("SELECT device FROM devices WHERE room_id=? AND device IN (%s) AND enabled=0" % placeholders, (room_id, *data.devices)).fetchall()
            if disabled:
                raise HTTPException(409, "Thiết bị %s đang bị Admin vô hiệu hóa" % disabled[0]["device"])
    transport = publish_room_command(room_id, data.devices)
    if FIRESTORE is not None:
        if not fs_room(room_id):
            raise HTTPException(404, "Không tìm thấy phòng")
        if transport["transport"] != "mqtt" or not transport["published"]:
            fs_update_room(room_id, {DEVICE_COLUMNS[key]: value for key, value in data.devices.items()})
        fs_add_audit(identity["user_id"], "DEVICE_COMMAND", "room=%s; %s" % (room_id, json.dumps(data.devices)))
        return {"ok": True, "room_id": room_id, "devices": data.devices, **transport}
    with db() as c:
        if not c.execute("SELECT id FROM rooms WHERE id=?", (room_id,)).fetchone():
            raise HTTPException(404, "Không tìm thấy phòng")
        if transport["transport"] != "mqtt" or not transport["published"]:
            assignments = [DEVICE_COLUMNS[key] + "=?" for key in data.devices]
            c.execute("UPDATE rooms SET " + ",".join(assignments) + " WHERE id=?", (*[int(v) for v in data.devices.values()], room_id))
        audit(c, identity["user_id"], "DEVICE_COMMAND", "room=%s; %s" % (room_id, json.dumps(data.devices)))
    return {"ok": True, "room_id": room_id, "devices": data.devices, **transport}


GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-20b")
GROQ_CHAT_URL = "https://api.groq.com/openai/v1/chat/completions"


def groq_chat_reply(message: str, identity: dict[str, str], session_id: str) -> Optional[str]:
    """Generate free-form replies for messages not handled by local rules."""
    api_key = os.getenv("GROQ_API_KEY", "").strip()
    if not api_key:
        return None
    cache_key = identity["user_id"] + ":" + hashlib.sha256(session_id.encode()).hexdigest()
    history = AI_CHAT.setdefault(cache_key, [])
    if len(history) > 16:
        del history[:-16]
    system_prompt = (
        "You are the friendly Smart Study Room assistant. Reply in the same language as the user's latest message. "
        "Keep answers direct, natural, and concise. You may answer questions beyond canned patterns, but never invent room prices, availability, bookings, or system policies. "
        "The application handles room lookup, prices, booking, cancellation, and booking steps using live data; never claim that you created or cancelled a booking. "
        "If a question requires private system data that was not provided, say so and direct the user to the relevant page feature. "
        "For questions outside room booking, you may answer with general knowledge and state when you are uncertain."
    )
    payload = {
        "model": GROQ_MODEL,
        "messages": [{"role": "system", "content": system_prompt}, *history, {"role": "user", "content": message[:2000]}],
        "max_completion_tokens": 400,
    }
    request = urllib.request.Request(
        GROQ_CHAT_URL,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": "Bearer " + api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
            # urllib's default Python-urllib user agent can be rejected by
            # upstream bot checks; identify this API client explicitly.
            "User-Agent": "SmartStudyRoom/1.0",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        result = json.loads(response.read().decode("utf-8"))
    choices = result.get("choices") or []
    content = choices[0].get("message", {}).get("content") if choices else None
    if isinstance(content, list):
        content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    if not isinstance(content, str) or not content.strip():
        return None
    answer = content.strip()
    history.extend([{"role": "user", "content": message[:2000]}, {"role": "assistant", "content": answer}])
    if len(history) > 16:
        del history[:-16]
    return answer


def groq_translate_chat_reply(user_message: str, reply: str) -> Optional[str]:
    """Translate a local canned response into the language used by the user."""
    api_key = os.getenv("GROQ_API_KEY", "").strip()
    if not api_key:
        return None
    payload = {
        "model": GROQ_MODEL,
        "messages": [
            {
                "role": "system",
                "content": (
                    "Translate the assistant reply into the main language used in the user's message. "
                    "Translate only; do not answer the user or add information. Preserve every number, date, time, price, "
                    "booking ID, room code, and proper name exactly. Output only the translated reply."
                ),
            },
            {
                "role": "user",
                "content": json.dumps({"user_message": user_message[:1000], "assistant_reply": reply[:2500]}, ensure_ascii=False),
            },
        ],
        "temperature": 0,
        "max_completion_tokens": 700,
    }
    request = urllib.request.Request(
        GROQ_CHAT_URL,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": "Bearer " + api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "SmartStudyRoom/1.0",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        result = json.loads(response.read().decode("utf-8"))
    choices = result.get("choices") or []
    content = choices[0].get("message", {}).get("content") if choices else None
    if isinstance(content, list):
        content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    return content.strip() if isinstance(content, str) and content.strip() else None


def groq_parse_booking_request(message: str, rooms: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Use Groq to translate a natural-language booking request into slots only.

    Availability, pricing, confirmation, and booking creation stay in local code.
    """
    api_key = os.getenv("GROQ_API_KEY", "").strip()
    if not api_key:
        return None
    today = now().astimezone().date().isoformat()
    room_context = [
        {"name": room.get("name"), "capacity": room.get("capacity"), "amenities": room.get("amenities", [])}
        for room in rooms
    ]
    prompt = (
        "Extract a user's room-booking request from any language. Return exactly one JSON object and no markdown. "
        "Use these keys: intent (book_room or other), people (integer or null), date (YYYY-MM-DD string or null), "
        "start_time and end_time (HH:MM strings or null), duration_minutes (integer or null), "
        "amenities (array of canonical names), and room_name (string or null). Example: "
        "{\"intent\":\"book_room\",\"people\":2,\"date\":\"2026-10-09\",\"start_time\":\"14:00\","
        "\"end_time\":\"16:00\",\"duration_minutes\":120,\"amenities\":[\"Quạt\",\"Đèn\"],\"room_name\":null}. "
        "Canonical amenities are the exact Vietnamese labels in room data; map light/lamp to Đèn, fan to Quạt, speaker to Loa. "
        "Resolve relative dates using today's local date " + today + ". 'This Friday' means the upcoming Friday (today if today is Friday); "
        "'next Friday' means the Friday of the following week. Convert 2pm to 14:00. "
        "Do not invent omitted booking details; use null. This is extraction only: never claim availability, price, confirmation, or booking creation. "
        "Room catalog: " + json.dumps(room_context, ensure_ascii=False)
    )
    payload = {
        "model": GROQ_MODEL,
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": message[:2000]},
        ],
        "temperature": 0,
        # Leave room for GPT-OSS reasoning plus the small JSON result.
        "max_completion_tokens": 1024,
        # JSON Object Mode avoids provider-specific JSON Schema validation quirks;
        # the application validates every extracted field before using it.
        "response_format": {"type": "json_object"},
    }
    if GROQ_MODEL.startswith("openai/gpt-oss-"):
        payload["reasoning_format"] = "hidden"
        payload["reasoning_effort"] = "low"
    request = urllib.request.Request(
        GROQ_CHAT_URL,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": "Bearer " + api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "SmartStudyRoom/1.0",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        result = json.loads(response.read().decode("utf-8"))
    choices = result.get("choices") or []
    content = choices[0].get("message", {}).get("content") if choices else None
    if isinstance(content, list):
        content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    if not isinstance(content, str):
        return None
    content = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", content.strip(), flags=re.I)
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", content, re.S)
        if not match:
            return None
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    return parsed if isinstance(parsed, dict) else None


def _logged_chat_reply(payload: dict, source: str = "local") -> dict:
    model = f" model={GROQ_MODEL}" if source == "groq" else ""
    print(f"CHAT_REPLY source={source}{model}", flush=True)
    user_message = CHAT_REQUEST_MESSAGE.get()
    if source != "groq" and isinstance(payload.get("reply"), str) and user_message:
        if not re.search(r"[ăâđêôơưàáảãạằắẳẵặầấẩẫậèéẻẽẹềếểễệìíỉĩịòóỏõọồốổỗộờớởỡợùúủũụừứửữựỳýỷỹỵ]", user_message.casefold()) and not re.search(
            r"\b(tôi|mình|minh|toi|phòng|phong|đặt|dat|cần|can|muốn|muon|ngày|ngay|giờ|gio|quạt|quat|đèn|den)\b",
            user_message.casefold(),
        ):
            try:
                translated = groq_translate_chat_reply(user_message, payload["reply"])
                if translated:
                    payload["reply"] = translated
                    print("CHAT_TRANSLATION source=groq", flush=True)
            except Exception as exc:
                detail = ""
                if hasattr(exc, "read"):
                    try:
                        detail = exc.read().decode("utf-8", errors="replace")[:300]
                    except Exception:
                        pass
                print("Groq translation unavailable (%s): %s%s" % (
                    type(exc).__name__, str(exc), ("; response=" + detail) if detail else ""
                ), flush=True)
    CHAT_REQUEST_MESSAGE.set(None)
    return payload


@app.post("/api/chat")
def chat(data: ChatIn, identity: dict[str, str] = Depends(current_user)):
    message = data.message.strip()
    CHAT_REQUEST_MESSAGE.set(message)
    state = CHAT.setdefault(identity["user_id"] + ":" + data.session_id, {})
    normalized = message.casefold()
    time_range_match = re.search(
        r"\btừ\s*(?P<from_hour>\d{1,2})(?:(?:h|:)(?P<from_min>\d{1,2})?|(?:\s*giờ))?\s*(?P<from_period>sáng|trưa|chiều|tối)?\s*(?:đến|tới|den|toi|[-–]|to)\s*(?P<to_hour>\d{1,2})(?:(?:h|:)(?P<to_min>\d{1,2})?|(?:\s*giờ))?\s*(?P<to_period>sáng|trưa|chiều|tối)?\b",
        normalized,
    )
    # Short greetings and acknowledgements are fixed UI copy: answer locally, without an AI call.
    if re.fullmatch(r"\s*(xin chào|chào(?: bạn| ad)?|hello|hi|hey)[!.\s]*", normalized):
        return _logged_chat_reply({"reply": "Chào bạn! 👋 Mình có thể giới thiệu phòng, báo giá hoặc giúp bạn tìm phòng phù hợp. Bạn muốn xem dịch vụ, loại phòng, bảng giá hay bắt đầu đặt phòng?"})
    if re.fullmatch(r"\s*(cảm ơn|cam on|cảm ơn nhé|thanks|thank you)[!.\s]*", normalized):
        return _logged_chat_reply({"reply": "Rất vui được hỗ trợ bạn! Nếu cần, mình có thể xem loại phòng, giá hoặc tìm phòng còn trống."})
    if re.fullmatch(r"\s*(tìm phòng phù hợp|tìm phòng|bắt đầu đặt phòng)[!.\s]*", normalized):
        state["local_booking_flow"] = True
        return _logged_chat_reply({"reply": "Mình sẽ tìm phòng phù hợp cho bạn. Trước tiên, bạn cần phòng cho bao nhiêu người? Sau đó cho mình ngày, giờ bắt đầu và thời lượng." , "needs": ["people", "date", "time", "duration"]})
    # Carry room type and amenity preferences across turns just like the other booking slots.
    available_rooms = fs_rooms() if FIRESTORE is not None else None
    if available_rooms is None:
        with db() as c:
            available_rooms = [sqlite_room(row) for row in c.execute("SELECT * FROM rooms").fetchall()]
    amenity_aliases = {
        "Đèn": ("đèn", "den", "lamp"), "Quạt": ("quạt", "quat", "fan"),
        "Loa": ("loa", "speaker"),
    }
    requested_amenities = set(state.get("amenities", []))
    for amenity, aliases in amenity_aliases.items():
        if any(alias in normalized for alias in aliases):
            requested_amenities.add(amenity)
    if requested_amenities:
        state["amenities"] = sorted(requested_amenities)
    room_aliases = {"lab": ("lab", "phòng thí nghiệm"), "thảo luận": ("thảo luận", "thao luan"),
                    "nhóm": ("phòng nhóm", "phòng cho nhóm"), "học": ("phòng học",)}
    room_filter = state.get("room_filter")
    code_match = re.search(r"\b([a-d]\d{3})\b", normalized)
    detected_filter = code_match.group(1).upper() if code_match else None
    if not detected_filter:
        for key, aliases in room_aliases.items():
            if any(alias in normalized for alias in aliases):
                detected_filter = key
                break
    if detected_filter:
        room_filter = detected_filter
        state["room_filter"] = room_filter

    # Default Vietnamese booking patterns below continue to use deterministic
    # local parsing. Send less structured / natural-language room requests to
    # Groq for slot extraction, then keep availability and confirmation local.
    natural_booking_hint = bool(
        re.search(r"\b(?:i\s+)?(?:want|need|book|reserve|find|get|looking\s+for)\b.{0,120}\broom\b", normalized)
        or re.search(r"\broom\b.{0,100}\b(?:for\s+\w+\s+(?:people|persons|guests)|from\s+\d|this\s+(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday))\b", normalized)
        or re.search(r"\b(?:mình|tôi|em|anh|chị)?\s*(?:muốn|cần|đặt|tìm|thuê)\b.{0,120}\bphòng\b", normalized)
    )
    groq_booking_slots = None
    if natural_booking_hint:
        if not os.getenv("GROQ_API_KEY", "").strip():
            return _logged_chat_reply({"reply": "Để hiểu yêu cầu đặt phòng tự nhiên này, app cần GROQ_API_KEY. Hãy cấu hình key rồi khởi động lại server."})
        try:
            groq_booking_slots = groq_parse_booking_request(message, available_rooms)
        except Exception as exc:
            detail = ""
            if hasattr(exc, "read"):
                try:
                    detail = exc.read().decode("utf-8", errors="replace")[:500]
                except Exception:
                    pass
            print("Groq booking parser unavailable (%s): %s%s" % (
                type(exc).__name__, str(exc), ("; response=" + detail) if detail else ""
            ), flush=True)
            status = getattr(exc, "code", None)
            error_hint = f"Groq trả HTTP {status}. " if status else ""
            return _logged_chat_reply({"reply": error_hint + "Mình chưa phân tích được yêu cầu đặt phòng. Hãy xem log terminal để biết chi tiết, hoặc gửi lại số người, ngày, giờ và tiện ích cần dùng nhé."})

        if not isinstance(groq_booking_slots, dict):
            print("Groq booking parser returned empty or invalid JSON", flush=True)
            return _logged_chat_reply({"reply": "Groq chưa trả được dữ liệu đặt phòng theo định dạng cần thiết. Hãy gửi lại yêu cầu hoặc nhập số người, ngày, giờ và tiện ích thành từng tin nhắn nhé."})

        if groq_booking_slots and str(groq_booking_slots.get("intent", "")).casefold() == "book_room":
            state["local_booking_flow"] = True
            people_value = groq_booking_slots.get("people")
            if isinstance(people_value, int) and not isinstance(people_value, bool) and 1 <= people_value <= 100:
                state["people"] = people_value
            date_value = groq_booking_slots.get("date")
            # Relative weekdays are deterministic; never let an LLM turn Friday
            # into a calendar date with the wrong weekday.
            weekday_names = {
                "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
                "friday": 4, "saturday": 5, "sunday": 6,
            }
            explicit_date_in_message = bool(
                re.search(r"\b20\d{2}[-/]\d{1,2}[-/]\d{1,2}\b", normalized)
                or re.search(r"\b\d{1,2}[/-]\d{1,2}[/-]20\d{2}\b", normalized)
            )
            english_weekday = re.search(
                r"\b(?:(this|next)\s+)?(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
                normalized,
            )
            if english_weekday and not explicit_date_in_message:
                local_today_for_weekday = now().astimezone().date()
                target_weekday = weekday_names[english_weekday.group(2)]
                weekday_offset = (target_weekday - local_today_for_weekday.weekday()) % 7
                if english_weekday.group(1) == "next":
                    weekday_offset += 7
                date_value = (local_today_for_weekday + timedelta(days=weekday_offset)).isoformat()
            if isinstance(date_value, str):
                try:
                    state["date"] = date.fromisoformat(date_value).isoformat()
                except ValueError:
                    pass
            start_value = groq_booking_slots.get("start_time")
            end_value = groq_booking_slots.get("end_time")
            time_pattern = r"^(?:[01]?\d|2[0-3]):[0-5]\d$"
            if isinstance(start_value, str) and re.fullmatch(time_pattern, start_value):
                state["time"] = start_value.zfill(5)
                start_minutes = int(start_value.split(":")[0]) * 60 + int(start_value.split(":")[1])
                duration_value = groq_booking_slots.get("duration_minutes")
                if isinstance(end_value, str) and re.fullmatch(time_pattern, end_value):
                    end_minutes = int(end_value.split(":")[0]) * 60 + int(end_value.split(":")[1])
                    if end_minutes <= start_minutes:
                        end_minutes += 24 * 60
                    duration_value = end_minutes - start_minutes
                if isinstance(duration_value, int) and not isinstance(duration_value, bool) and 15 <= duration_value <= 12 * 60:
                    state["duration_minutes"] = duration_value
            aliases_to_names = {
                "đèn": "Đèn", "den": "Đèn", "lamp": "Đèn", "light": "Đèn", "lights": "Đèn",
                "quạt": "Quạt", "quat": "Quạt", "fan": "Quạt", "fans": "Quạt",
                "loa": "Loa", "speaker": "Loa", "speakers": "Loa",
            }
            ai_amenities = groq_booking_slots.get("amenities")
            if isinstance(ai_amenities, list):
                for value in ai_amenities:
                    if isinstance(value, str) and value.casefold() in aliases_to_names:
                        requested_amenities.add(aliases_to_names[value.casefold()])
                if requested_amenities:
                    state["amenities"] = sorted(requested_amenities)
            ai_room_name = groq_booking_slots.get("room_name")
            if isinstance(ai_room_name, str) and ai_room_name.strip():
                matched_room = next((room for room in available_rooms if ai_room_name.casefold() in room.get("name", "").casefold()), None)
                if matched_room:
                    room_filter = matched_room.get("name")
                    state["room_filter"] = room_filter
            print("CHAT_PARSE source=groq intent=book_room model=%s" % GROQ_MODEL, flush=True)

    if re.search(r"(trạng thái|tình trạng).*(booking|đặt phòng)|booking.*(của tôi|nào|trạng thái)|đặt phòng của tôi", normalized):
        state.pop("pending_confirm", None)
        items = [b for b in fs_bookings() if b.get("user_id") == identity["user_id"]] if FIRESTORE is not None else list_bookings(identity)
        items = sorted(items, key=lambda booking: booking.get("created", ""), reverse=True)[:5]
        if not items:
            return _logged_chat_reply({"reply": "Bạn chưa có booking nào."})
        lines = ["• #%s · phòng %s · %s · %s" % (b["id"][:8], b["room_id"], b["status"], parse_dt(b["start"]).astimezone().strftime("%d/%m %H:%M")) for b in items]
        return _logged_chat_reply({"reply": "Booking gần đây của bạn:\n" + "\n".join(lines)})
    if re.search(r"(hủy|huy).*(booking|đặt phòng|phòng)|((booking|đặt phòng).*(hủy|huy))", normalized):
        state.pop("pending_confirm", None)
        items = [b for b in fs_bookings() if b.get("user_id") == identity["user_id"] and b.get("status") == "CONFIRMED"] if FIRESTORE is not None else [b for b in list_bookings(identity) if b.get("status") == "CONFIRMED"]
        short_id = re.search(r"\b([0-9a-f]{8})\b", normalized)
        if short_id:
            items = [b for b in items if b["id"].startswith(short_id.group(1))]
        if len(items) == 1:
            try:
                cancel_booking(items[0]["id"], identity)
                return _logged_chat_reply({"reply": "Đã hủy booking #%s." % items[0]["id"][:8]})
            except HTTPException as exc:
                return _logged_chat_reply({"reply": "Mình chưa thể hủy booking: " + str(exc.detail)})
        if items:
            return _logged_chat_reply({"reply": "Bạn có nhiều booking chưa check-in. Hãy gửi 8 ký tự đầu của mã booking muốn hủy:\n" + "\n".join("• #%s · phòng %s · %s" % (b["id"][:8], b["room_id"], parse_dt(b["start"]).astimezone().strftime("%d/%m %H:%M")) for b in items[:6])})
        return _logged_chat_reply({"reply": "Hiện không có booking CONFIRMED nào để hủy."})
    if re.search(r"(dịch vụ|bạn.*giúp.*gì|bạn làm được gì|có thể giúp|có những chức năng gì|cách đặt|check.?in.*qr)", normalized):
        state.pop("pending_confirm", None)
        return _logged_chat_reply({"reply": "Mình có thể giúp bạn:\n• Giới thiệu phòng, tiện nghi và giá hiện tại.\n• Tìm phòng còn trống theo số người, ngày, giờ và thời lượng.\n• Tạo booking và cấp QR check-in.\n• Điều khiển thiết bị phòng gồm đèn, quạt và loa. Hệ thống nhận telemetry/cập nhật trạng thái qua MQTT; nếu chưa cấu hình broker thì demo mô phỏng qua HTTP.\n\nBạn muốn xem dịch vụ thiết bị, loại phòng, bảng giá hay bắt đầu tìm phòng?"})
    if re.search(r"(bảng giá|chi phí|mức phí|bao nhiêu (?:tiền|đồng|một giờ)|giá\s*(?:phòng|bao nhiêu|thế nào|ra sao)|phòng\s+[a-d]\d{3}\s+giá)", normalized):
        if FIRESTORE is not None:
            rows = sorted(fs_rooms(), key=lambda room: room.get("hourly_rate", 0))
            room_code = re.search(r"\b([a-d]\d{3})\b", normalized)
            if room_code:
                rows = [room for room in rows if room_code.group(1).upper() in room.get("name", "").upper()]
            state.pop("pending_confirm", None)
            if not rows:
                return _logged_chat_reply({"reply": "Mình không tìm thấy phòng đó. Bạn có thể nhắn ‘bảng giá’ để xem giá các phòng hiện có."})
            lines = [f"• {r['name']}: {r['hourly_rate']:,}₫/giờ · tối đa {r['capacity']} người" for r in rows]
            return _logged_chat_reply({"reply": "Giá phòng hiện tại (theo cấu hình hệ thống):\n" + "\n".join(lines) + "\nBạn muốn mình tìm phòng còn trống vào ngày, giờ và thời lượng nào?"})
        with db() as c:
            rows = c.execute("SELECT name,capacity,amenities,hourly_rate FROM rooms ORDER BY hourly_rate").fetchall()
        room_code = re.search(r"\b([a-d]\d{3})\b", normalized)
        if room_code:
            rows = [r for r in rows if room_code.group(1).upper() in r["name"].upper()]
        state.pop("pending_confirm", None)
        if not rows:
            return _logged_chat_reply({"reply": "Mình không tìm thấy phòng đó. Bạn có thể nhắn ‘bảng giá’ để xem giá các phòng hiện có."})
        lines = [f"• {r['name']}: {r['hourly_rate']:,}₫/giờ · tối đa {r['capacity']} người" for r in rows]
        return _logged_chat_reply({"reply": "Giá phòng hiện tại (theo cấu hình hệ thống):\n" + "\n".join(lines) + "\nBạn muốn mình tìm phòng còn trống vào ngày, giờ và thời lượng nào?"})
    # Informational room questions use the database as the source of truth.
    if re.search(r"(các loại|loại.*phòng|phòng.*loại nào|có.*phòng.*(?:gì|nào)|giới thiệu.*phòng|thông tin.*phòng|danh sách phòng|phòng.*(?:tiện nghi|sức chứa))", normalized):
        state.pop("pending_confirm", None)
        if FIRESTORE is not None:
            rows = sorted(fs_rooms(), key=lambda room: room.get("capacity", 0))
            descriptions = [f"• {r['name']}: tối đa {r['capacity']} người; có {', '.join(r.get('amenities', [])) or 'thiết bị chưa cấu hình'}; {r['hourly_rate']:,}₫/giờ" for r in rows]
            return _logged_chat_reply({"reply": "Hiện có các phòng sau (thông tin lấy từ hệ thống):\n" + "\n".join(descriptions) + "\nBạn muốn đặt phòng nào? Hãy cho mình biết số người, ngày, giờ bắt đầu và thời lượng."})
        with db() as c:
            rows = [sqlite_room(row) for row in c.execute("SELECT * FROM rooms ORDER BY capacity").fetchall()]
        descriptions = [f"• {r['name']}: tối đa {r['capacity']} người; có {', '.join(r.get('amenities', [])) or 'thiết bị chưa cấu hình'}; {r['hourly_rate']:,}₫/giờ" for r in rows]
        return _logged_chat_reply({"reply": "Hiện có các phòng sau (thông tin lấy từ hệ thống):\n" + "\n".join(descriptions) + "\nBạn muốn đặt phòng nào? Hãy cho mình biết số người, ngày, giờ bắt đầu và thời lượng."})
    # Known answers and slot-based booking stay local. Groq generates answers
    # only for unrecognized free-form questions.
    has_booking_slot = bool(
        re.search(r"\b\d{1,2}\s*(?:người|nguoi|pax)\b", normalized)
        or re.search(r"\b(?:hôm nay|hom nay|ngày mai|ngay mai|mai|ngày kia|ngay kia|mốt|mot|20\d{2}[-/]\d{1,2}[-/]\d{1,2}|\d{1,2}[/-]\d{1,2}(?:[/-]20\d{2})?|thứ\s*[2-7]|chủ nhật)\b", normalized)
        or re.search(r"\b\d{1,2}\s*(?:h|:)\s*\d{0,2}\b", normalized)
        or time_range_match
        or re.search(r"\d+(?:[.,]\d+)?\s*(?:tiếng|giờ|hours|phút|phut|min|minutes)", normalized)
        or (groq_booking_slots and str(groq_booking_slots.get("intent", "")).casefold() == "book_room")
    )
    is_general_question = bool(
        re.search(r"(?<!\w)\d+(?:[.,]\d+)?\s*(?:\+|-|\*|/|×|÷)\s*\d+(?:[.,]\d+)?(?!\w)", normalized)
        or re.search(r"\b(?:bằng mấy|bằng bao nhiêu|là ai|là gì|tại sao|vì sao|giới thiệu bản thân|giải thích|tính giúp|tính toán)\b", normalized)
    )
    is_booking_reply = bool(
        re.fullmatch(r"\s*\d{1,2}\s*(?:(?:người|nguoi|pax))?\s*", normalized)
        or re.search(r"\b(?:xác nhận|đồng ý|confirm|đặt phòng|hủy|huy|bắt đầu đặt phòng|tìm phòng|đổi giờ|đổi ngày)\b", normalized)
        or detected_filter
        or any(alias in normalized for aliases in amenity_aliases.values() for alias in aliases)
        or re.fullmatch(r"\s*(?:ok|okay|được|dạ|vâng|ừ|rồi|yes|no)\s*[!.]*\s*", normalized)
    )
    should_ask_groq = (is_general_question or not has_booking_slot) and (
        not state.get("local_booking_flow") or is_general_question or not is_booking_reply
    )
    if should_ask_groq and is_general_question and not os.getenv("GROQ_API_KEY", "").strip():
        return _logged_chat_reply({"reply": "Câu này cần Groq để trả lời, nhưng app chưa nhận được GROQ_API_KEY. Hãy cấu hình key rồi khởi động lại server."})
    if os.getenv("GROQ_API_KEY", "").strip() and should_ask_groq:
        try:
            answer = groq_chat_reply(message, identity, data.session_id)
            if answer:
                return _logged_chat_reply({"reply": answer}, source="groq")
        except Exception as exc:
            # Known answers and the local booking flow still work if Groq is unavailable.
            detail = ""
            if hasattr(exc, "read"):
                try:
                    detail = exc.read().decode("utf-8", errors="replace")[:500]
                except Exception:
                    pass
            print("Groq chat unavailable (%s): %s%s" % (
                type(exc).__name__, str(exc), ("; response=" + detail) if detail else ""
            ), flush=True)
            if is_general_question:
                return _logged_chat_reply({"reply": "Mình chưa kết nối được Groq để trả lời câu này. Bạn thử gửi lại sau nhé."})

    if has_booking_slot:
        state["local_booking_flow"] = True

    people_match = re.search(r"\b(\d{1,2})\s*(?:người|nguoi|pax)\b", normalized)
    if not people_match and state.get("local_booking_flow") and not state.get("people"):
        people_match = re.fullmatch(r"\s*(\d{1,2})\s*", normalized)
    if people_match:
        state["people"] = int(people_match.group(1))

    local_today = now().astimezone().date()
    requested_date = None
    relative = re.search(r"\b(hôm nay|hom nay|ngày mai|ngay mai|mai|ngày kia|ngay kia|mốt|mot)\b", normalized)
    if relative:
        word = relative.group(1)
        offset = 0 if word in ("hôm nay", "hom nay") else (2 if word in ("ngày kia", "ngay kia", "mốt", "mot") else 1)
        requested_date = local_today + timedelta(days=offset)
    else:
        iso_match = re.search(r"\b(20\d{2})[-/](\d{1,2})[-/](\d{1,2})\b", normalized)
        dmy_match = re.search(r"\b(\d{1,2})[/-](\d{1,2})[/-](20\d{2})\b", normalized)
        short_dmy_match = re.search(r"\b(\d{1,2})[/-](\d{1,2})(?![/-]\d{2,4})\b", normalized)
        vn_match = re.search(r"ngày\s+(\d{1,2})\s+tháng\s+(\d{1,2})(?:\s+năm\s+(20\d{2}))?", normalized)
        weekday_match = re.search(r"\b(thứ\s*([2-7])|chủ nhật)\b", normalized)
        try:
            if iso_match:
                requested_date = date(int(iso_match.group(1)), int(iso_match.group(2)), int(iso_match.group(3)))
            elif dmy_match:
                requested_date = date(int(dmy_match.group(3)), int(dmy_match.group(2)), int(dmy_match.group(1)))
            elif short_dmy_match:
                requested_date = date(local_today.year, int(short_dmy_match.group(2)), int(short_dmy_match.group(1)))
            elif vn_match:
                requested_date = date(int(vn_match.group(3) or local_today.year), int(vn_match.group(2)), int(vn_match.group(1)))
            elif weekday_match:
                target_weekday = 6 if weekday_match.group(1) == "chủ nhật" else int(weekday_match.group(2)) - 2
                offset = (target_weekday - local_today.weekday()) % 7 or 7
                requested_date = local_today + timedelta(days=offset)
        except ValueError:
            return _logged_chat_reply({"reply": "Ngày bạn nhập không hợp lệ. Hãy ghi ngày/tháng/năm, ví dụ 05/10/2026.", "needs": ["date"]})
    if requested_date:
        if requested_date < local_today:
            state.pop("date", None); state.pop("start", None); state.pop("end", None); state.pop("pending_confirm", None)
            return _logged_chat_reply({"reply": "Ngày đó đã qua nên mình không thể tạo booking. Hãy chọn hôm nay hoặc một ngày trong tương lai.", "needs": ["date"]})
        state["date"] = requested_date.isoformat()

    if time_range_match:
        def range_time_minutes(hour_group: str, minute_group: str, period_group: str) -> int:
            hour = int(time_range_match.group(hour_group))
            minute = int(time_range_match.group(minute_group) or 0)
            period = time_range_match.group(period_group)
            if hour > 23 or minute > 59:
                raise ValueError("invalid time")
            if period in ("chiều", "tối") and hour < 12:
                hour += 12
            elif period == "sáng" and hour == 12:
                hour = 0
            elif period == "trưa" and hour < 11:
                hour += 12
            return hour * 60 + minute

        try:
            start_minutes = range_time_minutes("from_hour", "from_min", "from_period")
            end_minutes = range_time_minutes("to_hour", "to_min", "to_period")
        except ValueError:
            return _logged_chat_reply({"reply": "Khoảng giờ chưa hợp lệ. Hãy nhập ví dụ: từ 14:00 tới 16:30.", "needs": ["time"]})
        if end_minutes <= start_minutes:
            end_minutes += 24 * 60
        duration_minutes = end_minutes - start_minutes
        if duration_minutes < 15 or duration_minutes > 12 * 60:
            return _logged_chat_reply({"reply": "Thời lượng tính từ khoảng giờ phải từ 15 phút đến 12 tiếng. Hãy gửi lại giờ bắt đầu và giờ kết thúc.", "needs": ["time", "duration"]})
        state["time"] = f"{start_minutes // 60 % 24:02d}:{start_minutes % 60:02d}"
        state["duration_minutes"] = duration_minutes

    time_match = re.search(r"\b(?:lúc\s*)?(\d{1,2})\s*(?:h|:)(\d{2})?\b", normalized)
    if time_match:
        hour, minute = int(time_match.group(1)), int(time_match.group(2) or 0)
        if hour > 23 or minute > 59:
            return _logged_chat_reply({"reply": "Giờ không hợp lệ. Hãy nhập giờ từ 00:00 đến 23:59.", "needs": ["time"]})
        state["time"] = f"{hour:02d}:{minute:02d}"

    duration_match = re.search(r"(?:trong\s*)?(?:(\d+(?:[.,]\d+)?)\s*(tiếng|giờ|hours)(?:\s*(?:và\s*)?(\d+)\s*(?:phút|phut))?|(\d+)\s*(phút|phut|min|minutes))", normalized)
    if duration_match and not time_range_match:
        if duration_match.group(1):
            minutes = round(float(duration_match.group(1).replace(",", ".")) * 60) + int(duration_match.group(3) or 0)
        else:
            minutes = int(duration_match.group(4))
        if minutes < 15 or minutes > 12 * 60:
            return _logged_chat_reply({"reply": "Thời lượng đặt phòng phải từ 15 phút đến 12 tiếng. Bạn muốn dùng phòng bao lâu?", "needs": ["duration"]})
        state["duration_minutes"] = minutes

    if not state.get("people"):
        return _logged_chat_reply({"reply": "Bạn cần phòng cho bao nhiêu người?", "needs": ["people"]})
    if not state.get("date"):
        return _logged_chat_reply({"reply": "Bạn muốn đặt phòng ngày nào? Hãy ghi ngày/tháng/năm hoặc nói hôm nay/ngày mai.", "needs": ["date"]})
    if not state.get("time"):
        return _logged_chat_reply({"reply": "Bạn muốn bắt đầu lúc mấy giờ? Ví dụ 14:30.", "needs": ["time"]})
    if not state.get("duration_minutes"):
        return _logged_chat_reply({"reply": "Bạn muốn sử dụng phòng trong bao lâu? Ví dụ 90 phút, 2 tiếng hoặc 2 tiếng 30 phút.", "needs": ["duration"]})
    start_local = datetime.combine(date.fromisoformat(state["date"]), datetime.strptime(state["time"], "%H:%M").time()).astimezone()
    if start_local <= now().astimezone():
        state.pop("time", None); state.pop("start", None); state.pop("end", None); state.pop("pending_confirm", None)
        return _logged_chat_reply({"reply": "Giờ bắt đầu đó đã qua. Hãy chọn một giờ trong tương lai (hoặc đổi sang ngày khác).", "needs": ["time"]})
    state["start"] = start_local.astimezone(timezone.utc)
    state["end"] = state["start"] + timedelta(minutes=state["duration_minutes"])
    choices = free_rooms(state["start"], state["end"], state["people"], state.get("amenities"))
    if room_filter:
        choices = [room for room in choices if (room_filter.casefold() in room.get("name", "").casefold())]
    if not choices:
        inventory = available_rooms or []
        capacity_rooms = [room for room in inventory if int(room.get("capacity", 0)) >= state["people"]]
        requested_amenities = {value.casefold() for value in state.get("amenities", [])}
        amenity_rooms = [
            room for room in capacity_rooms
            if requested_amenities.issubset({value.casefold() for value in (room.get("amenities") or [])})
        ]
        matching_rooms = [
            room for room in amenity_rooms
            if not room_filter or room_filter.casefold() in room.get("name", "").casefold()
        ]

        if not inventory:
            reason = "Hiện hệ thống chưa có phòng nào được cấu hình."
        elif not capacity_rooms:
            max_capacity = max(int(room.get("capacity", 0)) for room in inventory)
            reason = f"Không có phòng đủ sức chứa {state['people']} người; phòng lớn nhất hiện chỉ có {max_capacity} chỗ."
        elif not amenity_rooms:
            available = sorted({
                amenity for room in capacity_rooms for amenity in (room.get("amenities") or [])
            })
            suggestion = " Có thể bỏ bớt tiện ích hoặc chọn tiện ích đang có: " + ", ".join(available) + "." if available else " Các phòng đủ chỗ hiện chưa được gắn tiện ích nào."
            reason = f"Không có phòng đủ chỗ đồng thời có đủ tiện ích {', '.join(state.get('amenities', []))}.{suggestion}"
        elif not matching_rooms:
            candidates = ", ".join(room.get("name", "Phòng") for room in amenity_rooms)
            reason = f"Không có phòng tên/loại '{room_filter}' thỏa các yêu cầu này. Phòng phù hợp về sức chứa và tiện ích hiện có: {candidates}."
        else:
            period_start = state["start"].astimezone().strftime("%d/%m %H:%M")
            period_end = state["end"].astimezone().strftime("%d/%m %H:%M")
            booked_names = ", ".join(room.get("name", "Phòng") for room in matching_rooms)
            reason = f"Có {len(matching_rooms)} phòng đáp ứng sức chứa và tiện ích ({booked_names}), nhưng tất cả đã có booking trùng khung {period_start}–{period_end}. Hãy đổi ngày/giờ hoặc bỏ bớt tiện ích để tìm thêm lựa chọn."
        return _logged_chat_reply({"reply": reason, "rooms": []})
    room = choices[0]
    duration_hours = (state["end"] - state["start"]).total_seconds() / 3600
    state.update({"room_id": room["id"], "cost": round(room["hourly_rate"] * duration_hours)})
    signature = (state["people"], state["date"], state["time"], state["duration_minutes"], tuple(state.get("amenities", [])), room_filter)
    if state.get("context_signature") is not None and state["context_signature"] != signature:
        state.pop("pending_confirm", None)
    state["context_signature"] = signature
    start_local, end_local = state["start"].astimezone(), state["end"].astimezone()
    summary = (f"{room['name']} cho {state['people']} người, ngày {start_local:%d/%m/%Y}, {start_local:%H:%M}–{end_local:%H:%M} "
               f"dự kiến {state['cost']:,}₫. Phòng đủ sức chứa, còn trống và có giá thấp nhất trong các lựa chọn.")
    if state.get("amenities"):
        summary += " Đáp ứng tiện nghi: " + ", ".join(state["amenities"]) + "."
    if re.search(r"\b(xác nhận|đồng ý|đặt phòng|confirm)\b", message, re.I):
        if state.get("pending_confirm"):
            try:
                selected_amenities = {str(value).casefold() for value in state.get("amenities", [])}
                amenity_ids = [str(item["id"]) for item in get_amenities() if item.get("name", "").casefold() in selected_amenities]
                booking = create_booking(BookingIn(room_id=room["id"], start=state["start"], end=state["end"], people=state["people"], amenity_ids=amenity_ids, idempotency_key="chat-" + uuid.uuid4().hex), identity)
            except HTTPException as exc:
                return _logged_chat_reply({"reply": "Không thể tạo booking: " + str(exc.detail), "rooms": choices})
            state.clear()
            return _logged_chat_reply({"reply": "Đã tạo booking " + booking["id"][:8] + ". Trạng thái CONFIRMED.", "booking": booking})
        state["pending_confirm"] = True
        return _logged_chat_reply({"reply": "Trước khi đặt, hãy xác nhận đề xuất: " + summary + " Nhắn ‘xác nhận’ để tiếp tục.", "rooms": choices})
    state["pending_confirm"] = True
    return _logged_chat_reply({"reply": "Đề xuất: " + summary + " Nếu phù hợp, nhắn ‘xác nhận’ để đặt.", "rooms": choices})
@app.get("/api/admin/audit")
def get_audit(identity: dict[str, str] = Depends(current_user)):
    require_admin(identity["role"])
    if FIRESTORE is not None:
        items = [doc.to_dict() for doc in FIRESTORE.collection("audit").stream()]
        return sorted(items, key=lambda item: item.get("at", ""), reverse=True)[:200]
    with db() as c:
        return [dict(r) for r in c.execute("SELECT * FROM audit ORDER BY id DESC LIMIT 200").fetchall()]


@app.get("/api/admin/devices")
def admin_devices(identity: dict[str, str] = Depends(current_user)):
    require_admin(identity["role"])
    if FIRESTORE is not None:
        return sorted([doc.to_dict() for doc in FIRESTORE.collection("devices").stream()], key=lambda row: (row.get("room_id", 0), row.get("device", "")))
    with db() as c:
        result = []
        for row in c.execute("SELECT d.*,r." + ",r.".join(DEVICE_COLUMNS.values()) + " FROM devices d JOIN rooms r ON r.id=d.room_id ORDER BY d.room_id,d.device").fetchall():
            item = dict(row)
            item["on"] = bool(item.pop(DEVICE_COLUMNS[item["device"]]))
            item["enabled"] = bool(item["enabled"])
            result.append(item)
        return result


@app.patch("/api/admin/devices/{room_id}/{device}")
def configure_device(room_id: int, device: str, values: DeviceConfigIn, identity: dict[str, str] = Depends(current_user)):
    require_admin(identity["role"])
    if device not in DEVICE_COLUMNS:
        raise HTTPException(404, "Không tìm thấy loại thiết bị")
    updates = values.model_dump(exclude_unset=True)
    if not updates or ("name" in updates and (not updates["name"].strip() or len(updates["name"]) > 40)):
        raise HTTPException(400, "Cần tên thiết bị hợp lệ hoặc trạng thái enabled")
    if FIRESTORE is not None:
        ref = FIRESTORE.collection("devices").document("%s_%s" % (room_id, device))
        if not ref.get().exists:
            raise HTTPException(404, "Không tìm thấy thiết bị")
        ref.update(updates)
        fs_add_audit(identity["user_id"], "DEVICE_CONFIG_UPDATED", "room=%s; device=%s; %s" % (room_id, device, json.dumps(updates, ensure_ascii=False)))
    else:
        with db() as c:
            columns = ",".join(key + "=?" for key in updates)
            result = c.execute("UPDATE devices SET " + columns + " WHERE room_id=? AND device=?", (*updates.values(), room_id, device))
            if result.rowcount == 0:
                raise HTTPException(404, "Không tìm thấy thiết bị")
            audit(c, identity["user_id"], "DEVICE_CONFIG_UPDATED", "room=%s; device=%s; %s" % (room_id, device, json.dumps(updates, ensure_ascii=False)))
    return {"ok": True, "room_id": room_id, "device": device, **updates}


@app.patch("/api/admin/rooms/{room_id}")
def update_room(room_id: int, values: dict[str, Any], identity: dict[str, str] = Depends(current_user)):
    require_admin(identity["role"])
    allowed = {"name", "capacity", "hourly_rate"}
    if not values or set(values) - allowed:
        raise HTTPException(400, "Chỉ cập nhật name, capacity, hourly_rate")
    if ("capacity" in values and int(values["capacity"]) < 1) or ("hourly_rate" in values and int(values["hourly_rate"]) < 0):
        raise HTTPException(400, "Sức chứa hoặc giá không hợp lệ")
    if FIRESTORE is not None:
        if not fs_update_room(room_id, values):
            raise HTTPException(404, "Không tìm thấy phòng")
        fs_add_audit("admin", "ROOM_UPDATED", "room=" + str(room_id))
        return {"ok": True, "room_id": room_id}
    update = values
    clause = ",".join(k + "=?" for k in update)
    with db() as c:
        result = c.execute("UPDATE rooms SET " + clause + " WHERE id=?", (*update.values(), room_id))
        if result.rowcount == 0:
            raise HTTPException(404, "Không tìm thấy phòng")
        audit(c, "admin", "ROOM_UPDATED", "room=" + str(room_id))
        return {"ok": True, "room_id": room_id}


@app.get("/api/admin/bookings")
def admin_bookings(identity: dict[str, str] = Depends(current_user)):
    require_admin(identity["role"])
    if FIRESTORE is not None:
        rooms_by_id = {r["id"]: r["name"] for r in fs_rooms()}
        items = [dict(booking_view(b), room_name=rooms_by_id.get(b.get("room_id"), "Phòng")) for b in fs_bookings()]
        return sorted(items, key=lambda booking: booking.get("created", ""), reverse=True)
    with db() as c:
        return [dict(booking_view(r), room_name=r["room_name"]) for r in c.execute("SELECT b.*,r.name room_name FROM bookings b JOIN rooms r ON r.id=b.room_id ORDER BY b.created DESC").fetchall()]
