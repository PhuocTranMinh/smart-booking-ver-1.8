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

App dùng 4 collection cốt lõi:

| Collection | Nội dung |
|---|---|
| `rooms` | Tên, sức chứa, giá, tiện ích, telemetry và cấu hình thiết bị |
| `bookings` | Người đặt, phòng, thời gian, giá, trạng thái, tiện ích, hạn QR và lịch sử |
| `audit` | Nhật ký thao tác quan trọng |
| `app_meta` | Danh mục tiện ích, schema/migration và tài khoản đăng ký trong nhánh con `auth/users` |

Trước khi migrate, deploy code mới để app không còn ghi vào collection cũ. Sau đó xem trước dữ liệu cần gộp và dọn:

```powershell
.venv\Scripts\python.exe scripts\simplify_firestore.py --credentials .\firebase-service-account.json --project smart-booking-82438
```

Kiểm tra số phòng và booking. Nếu đúng, chạy migration; script sao lưu dữ liệu trước khi chuyển trạng thái/thiết bị vào phòng, lịch sử vào booking và xóa các collection cũ:

```powershell
.venv\Scripts\python.exe scripts\simplify_firestore.py --credentials .\firebase-service-account.json --project smart-booking-82438 --apply --backup .\firestore-before-compact.json
```

Sau migration, Firestore chỉ còn bốn collection cấp cao nhất: `rooms`, `bookings`, `audit`, `app_meta`. Tài khoản đăng ký được đặt trong subcollection `app_meta/auth/users`, mật khẩu được băm trước khi lưu; tài khoản mới chỉ có quyền user. Giữ JSON backup ở nơi an toàn; file có thể chứa cấu hình và lịch sử booking.

## Kết nối ứng dụng

Sau khi upload, chạy app local với cùng service account:

```powershell
$env:GOOGLE_APPLICATION_CREDENTIALS = (Resolve-Path .\firebase-service-account.json).Path
$env:FIREBASE_PROJECT_ID = "smart-booking-82438"
.venv\Scripts\python.exe -m uvicorn app:app --reload
```

Mở `http://127.0.0.1:8000/api/health`; cần thấy database là `firestore` và `firestore_connected` là `true`.

Trên Render, tạo Secret File tên `firebase-service-account.json` và đặt `GOOGLE_APPLICATION_CREDENTIALS=/etc/secrets/firebase-service-account.json`, `FIREBASE_PROJECT_ID=smart-booking-82438`, rồi redeploy. Không dùng key local trong repository.
