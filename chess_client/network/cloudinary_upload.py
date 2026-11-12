import mimetypes
import os
from pathlib import Path

import requests


CLOUDINARY_CLOUD_NAME = os.getenv("CLOUDINARY_CLOUD_NAME", "dyfmgr6vl")
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
UPLOAD_TYPES = {
    "image": {
        "extensions": {".jpg", ".jpeg", ".png", ".gif", ".webp"},
        "resource_type": "image"
    },
    "video": {
        "extensions": {".mp4", ".mov", ".webm"},
        "resource_type": "video"
    },
    "audio": {
        "extensions": {".wav", ".mp3", ".m4a", ".ogg", ".webm"},
        "resource_type": "video"
    },
    "document": {
        "extensions": {".pdf", ".doc", ".docx"},
        "resource_type": "raw"
    }
}


def describe_upload(path):
    file_path = Path(path)
    suffix = file_path.suffix.lower()
    category = next(
        (
            name for name, spec in UPLOAD_TYPES.items()
            if suffix in spec["extensions"]
        ),
        None
    )
    if category is None:
        raise ValueError("Chỉ nhận ảnh, video, PDF, Word và tệp ghi âm phổ biến.")

    size = file_path.stat().st_size
    if not 0 < size <= MAX_UPLOAD_BYTES:
        raise ValueError("Tệp phải có dung lượng từ 1 byte đến 25 MB.")

    mime_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
    if category == "document" and suffix == ".doc":
        mime_type = "application/msword"
    elif category == "document" and suffix == ".docx":
        mime_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    elif category == "audio" and suffix == ".m4a":
        mime_type = "audio/mp4"

    return {
        "path": file_path,
        "filename": file_path.name,
        "category": category,
        "resource_type": UPLOAD_TYPES[category]["resource_type"],
        "mime_type": mime_type,
        "bytes": size
    }


def upload_file(path, signature):
    upload = describe_upload(path)
    if signature.get("resource_type") != upload["resource_type"]:
        raise ValueError("Loại chữ ký Cloudinary không khớp với tệp.")
    if signature.get("cloud_name") != CLOUDINARY_CLOUD_NAME:
        raise ValueError("Cloudinary cloud name không khớp cấu hình.")

    resource_type = upload["resource_type"]
    url = (
        f"https://api.cloudinary.com/v1_1/{CLOUDINARY_CLOUD_NAME}/"
        f"{resource_type}/upload"
    )
    fields = {
        key: signature[key]
        for key in (
            "api_key",
            "timestamp",
            "signature",
            "public_id",
            "overwrite",
            "upload_preset"
        )
    }
    with upload["path"].open("rb") as file_stream:
        response = requests.post(
            url,
            data=fields,
            files={
                "file": (
                    upload["filename"],
                    file_stream,
                    upload["mime_type"]
                )
            },
            timeout=(10, 180)
        )
    try:
        result = response.json()
    except requests.exceptions.JSONDecodeError as exc:
        raise ValueError("Cloudinary trả về dữ liệu không hợp lệ.") from exc
    if not isinstance(result, dict):
        raise ValueError("Cloudinary trả về dữ liệu không hợp lệ.")
    if response.status_code >= 400 or result.get("error"):
        error = result.get("error", {})
        message = error.get("message", "Không thể tải tệp lên Cloudinary.")
        raise ValueError(message)

    secure_url = result.get("secure_url")
    public_id = result.get("public_id")
    uploaded_bytes = result.get("bytes")
    file_format = result.get("format")
    if (
        not isinstance(secure_url, str)
        or not isinstance(public_id, str)
        or not isinstance(uploaded_bytes, int)
        or not isinstance(file_format, str)
    ):
        raise ValueError("Cloudinary trả về thiếu thông tin tệp.")
    allowed_formats = {
        suffix.lstrip(".")
        for suffix in UPLOAD_TYPES[upload["category"]]["extensions"]
    }
    if (
        not secure_url.startswith(
            f"https://res.cloudinary.com/{CLOUDINARY_CLOUD_NAME}/"
        )
        or public_id != signature.get("public_id")
        or not 1 <= uploaded_bytes <= MAX_UPLOAD_BYTES
        or file_format.lower() not in allowed_formats
    ):
        raise ValueError("Cloudinary trả về tệp không khớp với nội dung đã chọn.")

    return {
        "filename": upload["filename"],
        "category": upload["category"],
        "resource_type": resource_type,
        "public_id": public_id,
        "secure_url": secure_url,
        "format": file_format,
        "bytes": uploaded_bytes
    }
