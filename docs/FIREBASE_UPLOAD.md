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

| Collection | Nội dung |
|---|---|
| `users` | Tài khoản đăng ký. Mật khẩu chỉ lưu dạng băm; không mở quyền đọc trực tiếp từ trình duyệt. |
| `rooms` | Tên, sức chứa, giá, trạng thái mới nhất, cấu hình thiết bị và danh sách tiện ích để đọc nhanh. |
| `amenities` | Danh mục Đèn, Quạt, Loa và giá theo giờ. |
| `room_amenities` | Liên kết nhiều-nhiều phòng ↔ tiện ích. |
| `devices` | Cấu hình và trạng thái mới nhất của từng thiết bị. |
| `bookings` | Người đặt, phòng, khoảng thời gian, chi phí, trạng thái và tiện ích đã tính tiền. |
| `qr_tokens` | Một bản ghi mỗi booking; chỉ lưu SHA-256 token, phòng, hạn dùng, trạng thái và thời gian sử dụng. |
| `booking_events` | Lịch sử tạo, hủy, check-in, hoàn tất và no-show; không nhồi mảng lịch sử vào booking mới. |
| `device_logs` | Lịch sử thay đổi trạng thái telemetry/điều khiển; ghi khi trạng thái đổi để giảm ghi lặp. |
| `idempotency` | Hash của idempotency key và booking kết quả để request gửi lại không tạo booking trùng. |
| `audit` | Nhật ký thao tác quản trị và hoạt động hệ thống. |
| `app_meta` | Cấu hình và mốc migration, không lưu danh mục tiện ích. |

Tạo booking kiểm tra xung đột trong Firestore transaction. Firestore không có exclusion constraint như PostgreSQL; backend dùng transaction và kiểm tra lịch đang giữ phòng. Các collection cũ sẽ được giữ lại trừ hai collection dư `room_status` và `events`; trước khi xóa, script tạo file backup đầy đủ.

Index tổng hợp cho truy vấn booking của user nằm trong `firestore.indexes.json` (`user_id` + `status`). Các index đơn như `booking_id`, `room_id`, `token_hash` và `at` dùng index mặc định của Firestore.

Xem trước migration:

```powershell
.venv\Scripts\python.exe scripts\simplify_firestore.py --credentials .\firebase-service-account.json --project smart-booking-82438
```

Kiểm tra số phòng, booking, tiện ích và lịch sử sự kiện. Nếu đúng, chạy migration có backup:

```powershell
.venv\Scripts\python.exe scripts\simplify_firestore.py --credentials .\firebase-service-account.json --project smart-booking-82438 --apply --backup .\firestore-before-compact.json
```

Script chuẩn hóa danh mục tiện ích và booking events, rồi chỉ xóa `room_status` (trạng thái mới nhất đã nằm trong `rooms`) và `events` (được chuyển sang `booking_events`). Collection `amenities`, `room_amenities`, `devices`, `booking_events`, `idempotency`, `users`, `qr_tokens` và `device_logs` được giữ lại. App dùng `users/{username}` cho tài khoản mới và vẫn cho tài khoản cũ trong `app_meta/auth/users` đăng nhập. Giữ JSON backup ở nơi an toàn; file có thể chứa cấu hình và lịch sử booking.

## Kết nối ứng dụng

Sau khi upload, chạy app local với cùng service account:

```powershell
$env:GOOGLE_APPLICATION_CREDENTIALS = (Resolve-Path .\firebase-service-account.json).Path
$env:FIREBASE_PROJECT_ID = "smart-booking-82438"
.venv\Scripts\python.exe -m uvicorn app:app --reload
```

Mở `http://127.0.0.1:8000/api/health`; cần thấy database là `firestore` và `firestore_connected` là `true`.

Trên Render, tạo Secret File tên `firebase-service-account.json` và đặt `GOOGLE_APPLICATION_CREDENTIALS=/etc/secrets/firebase-service-account.json`, `FIREBASE_PROJECT_ID=smart-booking-82438`, rồi redeploy. Không dùng key local trong repository.
