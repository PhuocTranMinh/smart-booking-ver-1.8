# Upload dữ liệu lên Firebase Firestore

## 1. Chuẩn bị service account

Trong Firebase Console, mở **Project settings → Service accounts**, tạo service account riêng cho ứng dụng và cấp quyền **Cloud Datastore User** (`roles/datastore.user`). Tải key JSON về máy, đặt tên `firebase-service-account.json` trong thư mục dự án.

Không gửi key qua chat, không chép key vào source code, và không đưa file key vào GitHub/ZIP. `.gitignore` đã bỏ qua `firebase-service-account*.json`.

## 2. Kiểm tra trước khi ghi

Mở PowerShell tại thư mục dự án. Lệnh sau kết nối Firebase và chỉ in số document sẽ thêm/đã tồn tại; nó chưa ghi gì:

```powershell
.venv\Scripts\python.exe scripts\upload_to_firebase.py --credentials .\firebase-service-account.json --project smart-booking-82438
```

Kiểm tra dòng **Target project** là `smart-booking-82438` và bảng số lượng khớp dữ liệu local.

## 3. Tải dữ liệu

Khi bảng xem trước đúng, thêm `--apply`:

```powershell
.venv\Scripts\python.exe scripts\upload_to_firebase.py --credentials .\firebase-service-account.json --project smart-booking-82438 --apply
```

Mặc định script chỉ thêm document chưa có, nên có thể chạy lại để tiếp tục sau khi upload bị gián đoạn. Nó không xóa collection và không sửa document đã tồn tại. Nếu chủ động muốn SQLite ghi đè document trùng ID, thêm `--overwrite`. Muốn dùng database khác, thêm `--sqlite .\duong-dan\rooms.db`.

## Dữ liệu được tổ chức

Script tải các collection theo thực thể của đề tài:

| Collection | Nội dung |
|---|---|
| `users` | ID demo và vai trò; không chứa mật khẩu |
| `rooms` | Tên, sức chứa, giá và trạng thái phòng |
| `room_status` | Telemetry hiện thời: online/offline, occupancy, trạng thái thiết bị, thời điểm cập nhật |
| `amenities` | Danh mục Đèn, Quạt, Loa |
| `room_amenities` | Liên kết riêng giữa phòng và tiện ích |
| `devices` | Thiết bị, trạng thái và topic MQTT của từng phòng |
| `bookings` | Người đặt, phòng, thời gian, giá, trạng thái, tiện ích và hạn QR |
| `idempotency` | Khóa chống tạo booking lặp |
| `booking_events` | Lịch sử chuyển trạng thái booking |
| `audit` | Nhật ký thao tác quan trọng |
| `app_meta` | Phiên bản schema/migration để app không tạo lại dữ liệu đã tải |

Document `rooms/{room_id}` có danh sách tiện ích để tra cứu nhanh; collection `room_amenities` giữ liên kết chuẩn cho thao tác quản trị. `room_status/{room_id}` là bản trạng thái hiện thời riêng, được đồng bộ theo telemetry, check-in và kết thúc booking. Document dùng ID ổn định lấy từ SQLite (liên kết tiện ích/thiết bị dùng dạng `room_id_amenity_id`), vì vậy upload lặp không tạo bản sao. Script ghi marker schema để app không xóa/gán lại liên kết tiện ích khi khởi động. Backend tiếp tục xử lý booking bằng Firestore transaction và cập nhật document phòng trong transaction để đồng bộ các yêu cầu cạnh tranh.

## Kết nối ứng dụng

Sau khi upload, chạy app local với cùng service account:

```powershell
$env:GOOGLE_APPLICATION_CREDENTIALS = (Resolve-Path .\firebase-service-account.json).Path
$env:FIREBASE_PROJECT_ID = "smart-booking-82438"
.venv\Scripts\python.exe -m uvicorn app:app --reload
```

Mở `http://127.0.0.1:8000/api/health`; cần thấy database là `firestore` và `firestore_connected` là `true`.

Trên Render, tạo Secret File tên `firebase-service-account.json` và đặt `GOOGLE_APPLICATION_CREDENTIALS=/etc/secrets/firebase-service-account.json`, `FIREBASE_PROJECT_ID=smart-booking-82438`, rồi redeploy. Không dùng key local trong repository.
