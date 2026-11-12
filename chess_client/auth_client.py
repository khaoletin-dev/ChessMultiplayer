import requests

# Thay bằng Web API Key thật dự án Firebase của bạn
FIREBASE_API_KEY = "AIzaSyDMCAK0PCJhBzE8Cd5rmWR93V2rX3xKGt4"
AUTH_TIMEOUT = (5, 15)


def _request_firebase_auth(operation, endpoint, email, password):
    url = f"https://identitytoolkit.googleapis.com/v1/accounts:{endpoint}?key={FIREBASE_API_KEY}"
    payload = {
        "email": email,
        "password": password,
        "returnSecureToken": True
    }

    try:
        response = requests.post(url, json=payload, timeout=AUTH_TIMEOUT)
        data = response.json()
    except requests.Timeout as exc:
        raise ConnectionError("Firebase không phản hồi. Hãy thử lại sau.") from exc
    except requests.exceptions.JSONDecodeError as exc:
        raise ValueError("Firebase trả về dữ liệu không hợp lệ.") from exc
    except requests.RequestException as exc:
        raise ConnectionError(f"Không thể kết nối Firebase: {exc}") from exc

    if not isinstance(data, dict):
        raise ValueError("Firebase trả về dữ liệu không hợp lệ.")
    if "error" in data:
        error_msg = data["error"].get("message", "Lỗi xác thực không xác định.")
        raise ValueError(f"{operation} thất bại: {error_msg}")
    response.raise_for_status()
    return data


def login_user(email, password):
    """Đăng nhập Firebase và trả thông tin định danh."""
    data = _request_firebase_auth(
        "Đăng nhập", "signInWithPassword", email, password
    )
    return {
        "idToken": data["idToken"],
        "localId": data["localId"],
        "email": data["email"]
    }


def register_user(email, password):
    """Đăng ký tài khoản Firebase mới."""
    return _request_firebase_auth("Đăng ký", "signUp", email, password)