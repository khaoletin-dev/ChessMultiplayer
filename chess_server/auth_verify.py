import os
from pathlib import Path

import firebase_admin
from firebase_admin import credentials, auth

service_account_path = Path(
    os.getenv("FIREBASE_SERVICE_ACCOUNT", "firebase_service_acc.json")
)
if not service_account_path.is_absolute():
    service_account_path = Path(__file__).parent / service_account_path
SERVICE_ACCOUNT_FILE = service_account_path
if not SERVICE_ACCOUNT_FILE.is_file():
    raise FileNotFoundError(
        f"Không tìm thấy Firebase service account: {SERVICE_ACCOUNT_FILE}"
    )

try:
    firebase_admin.get_app()
except ValueError:
    try:
        firebase_admin.initialize_app(
            credentials.Certificate(str(SERVICE_ACCOUNT_FILE))
        )
    except Exception as exc:
        raise RuntimeError("Không thể khởi tạo Firebase Admin SDK.") from exc
    print("[+] Đã kết nối thành công tới hệ thống Firebase Admin.")

def verify_client_token(id_token):
    """
    Hàm nhận idToken từ gói tin TCP của Client.
    Trả về UID nếu hợp lệ, hoặc báo lỗi nếu Token giả mạo/hết hạn.
    """
    try:
        # verify_id_token tự động kết nối với Google để check chữ ký điện tử
        decoded_token = auth.verify_id_token(id_token)
        
        # Trích xuất thông tin định danh
        uid = decoded_token['uid']
        email = decoded_token.get('email', 'unknown')
        
        return {
            "is_valid": True,
            "uid": uid,
            "email": email
        }
    except Exception as e:
        # Lỗi có thể do Token hết hạn (quá 1 tiếng) hoặc bị chỉnh sửa
        return {
            "is_valid": False,
            "error_message": str(e)
        }