import socket
import json
import threading
import time
import os
import hmac
import hashlib
import uuid
from collections import defaultdict, deque

if __package__:
    from .env_config import load_workspace_env
else:
    from env_config import load_workspace_env

load_workspace_env()

from auth_verify import verify_client_token
from database.user_db import (
    get_game_record,
    get_game_records,
    get_shared_game_record,
    get_or_create_user_profile,
    get_lobby_chat_messages,
    get_match_chat_messages,
    save_match_chat_message_for_players,
    save_game_record_for_players,
    save_lobby_chat_message
)

MASTER_TCP_PORT = int(os.getenv("MASTER_TCP_PORT", "8000"))
MASTER_UDP_PORT = int(os.getenv("MASTER_UDP_PORT", "9000"))
INTERNAL_SERVER_TOKEN = os.getenv("CHESS_INTERNAL_SERVER_TOKEN", "")
CLOUDINARY_CLOUD_NAME = os.getenv("CLOUDINARY_CLOUD_NAME", "dyfmgr6vl")
CLOUDINARY_API_KEY = os.getenv("CLOUDINARY_API_KEY", "")
CLOUDINARY_API_SECRET = os.getenv("CLOUDINARY_API_SECRET", "")
CLOUDINARY_UPLOAD_PRESET = os.getenv("CLOUDINARY_UPLOAD_PRESET", "")
MAX_CHAT_ATTACHMENT_BYTES = 25 * 1024 * 1024
MAX_MESSAGE_BYTES = 1024 * 1024
# ==========================================
# BIẾN TOÀN CỤC LƯU TRỮ TRẠNG THÁI
# ==========================================
# Danh sách User đang ở Sảnh: { client_socket: {"uid": "...", "profile": {...}} }
authenticated_users = {} 

# Danh sách Game Node đang rảnh: { "IP:Port": {"ip": "...", "game_port": 8001, "active_rooms": 0, "last_seen": 1690000000} }
active_nodes = {}
waiting_rooms = {}
users_lock = threading.RLock()
nodes_lock = threading.RLock()
rooms_lock = threading.RLock()
socket_send_lock = threading.Lock()
cloudinary_signatures = defaultdict(deque)
cloudinary_signatures_lock = threading.Lock()


def send_json_message(client_socket, message):
    payload = json.dumps(message).encode('utf-8') + b'\n'
    with socket_send_lock:
        client_socket.sendall(payload)


def receive_json_message(client_socket, buffered_data):
    while b'\n' not in buffered_data:
        chunk = client_socket.recv(2048)
        if not chunk:
            return None, buffered_data
        buffered_data += chunk
        if len(buffered_data) > MAX_MESSAGE_BYTES:
            raise ValueError("Gói tin từ client vượt quá giới hạn cho phép.")

    message_data, buffered_data = buffered_data.split(b'\n', 1)
    if not message_data:
        return receive_json_message(client_socket, buffered_data)
    return json.loads(message_data.decode('utf-8')), buffered_data


def validate_game_record(record):
    if not isinstance(record, dict):
        return "Dữ liệu hồ sơ trận không hợp lệ."
    if (
        not isinstance(record.get("match_id"), str)
        or not record["match_id"]
        or len(record["match_id"]) > 64
        or record.get("mode") not in ("1vs1", "2vs2")
        or record.get("result") not in ("1-0", "0-1", "1/2-1/2")
        or not isinstance(record.get("reason"), str)
        or len(record["reason"]) > 500
        or not isinstance(record.get("completed_at"), str)
        or len(record["completed_at"]) > 64
        or not isinstance(record.get("moves"), list)
        or len(record["moves"]) > 2000
        or not isinstance(record.get("players"), list)
        or len(record["players"]) != (4 if record["mode"] == "2vs2" else 2)
    ):
        return "Dữ liệu hồ sơ trận thiếu trường hoặc vượt giới hạn."

    seen_uids = set()
    for player in record["players"]:
        if (
            not isinstance(player, dict)
            or not isinstance(player.get("uid"), str)
            or not player["uid"]
            or len(player["uid"]) > 256
            or not isinstance(player.get("username"), str)
            or not player["username"].strip()
            or len(player["username"]) > 100
            or player.get("team") not in ("white", "black")
            or not isinstance(player.get("is_leader"), bool)
            or player["uid"] in seen_uids
        ):
            return "Danh sách người chơi trong hồ sơ trận không hợp lệ."
        seen_uids.add(player["uid"])

    for move in record["moves"]:
        if (
            not isinstance(move, dict)
            or not isinstance(move.get("uci"), str)
            or len(move["uci"]) > 10
            or not isinstance(move.get("san"), str)
            or len(move["san"]) > 20
        ):
            return "Danh sách nước đi trong hồ sơ trận không hợp lệ."
    return None


def _save_internal_game_record(client_socket, request, addr):
    if not INTERNAL_SERVER_TOKEN:
        send_json_message(client_socket, {
            "status": "error",
            "message": "Master chưa cấu hình CHESS_INTERNAL_SERVER_TOKEN."
        })
        return

    record = request.get("record")
    node_port = request.get("node_port")
    timestamp = request.get("timestamp")
    signature = request.get("signature")
    validation_error = validate_game_record(record)
    if validation_error:
        send_json_message(client_socket, {
            "status": "error",
            "message": validation_error
        })
        return
    if (
        not isinstance(node_port, int)
        or isinstance(node_port, bool)
        or not 1 <= node_port <= 65535
        or not isinstance(timestamp, int)
        or abs(time.time() - timestamp) > 60
        or not isinstance(signature, str)
    ):
        send_json_message(client_socket, {
            "status": "error",
            "message": "Thông tin xác thực Game Node không hợp lệ."
        })
        return

    with nodes_lock:
        trusted_node = any(
            node["ip"] == addr[0]
            and node["game_port"] == node_port
            and time.time() - node["last_seen"] <= 15
            for node in active_nodes.values()
        )
    if not trusted_node:
        send_json_message(client_socket, {
            "status": "error",
            "message": "Game Node chưa đăng ký heartbeat với Master."
        })
        return

    record_json = json.dumps(record, sort_keys=True, separators=(",", ":"))
    signature_payload = f"{timestamp}.{record_json}".encode("utf-8")
    expected_signature = hmac.new(
        INTERNAL_SERVER_TOKEN.encode("utf-8"),
        signature_payload,
        hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(signature, expected_signature):
        send_json_message(client_socket, {
            "status": "error",
            "message": "Chữ ký hồ sơ trận không hợp lệ."
        })
        return

    save_game_record_for_players(record)
    print(f"[DB] Đã lưu hồ sơ trận {record['match_id']} cho người chơi.")
    send_json_message(client_socket, {"status": "success"})


def _save_internal_match_chat_message(client_socket, request, addr):
    if not INTERNAL_SERVER_TOKEN:
        raise RuntimeError(
            "Master chưa cấu hình CHESS_INTERNAL_SERVER_TOKEN."
        )

    node_port = request.get("node_port")
    timestamp = request.get("timestamp")
    payload = request.get("payload")
    signature = request.get("signature")
    if (
        not isinstance(node_port, int)
        or isinstance(node_port, bool)
        or not 1 <= node_port <= 65535
        or not isinstance(timestamp, int)
        or abs(time.time() - timestamp) > 60
        or not isinstance(payload, dict)
        or not isinstance(signature, str)
    ):
        raise ValueError("Thông tin xác thực Game Node không hợp lệ.")

    with nodes_lock:
        trusted_node = any(
            node["ip"] == addr[0]
            and node["game_port"] == node_port
            and time.time() - node["last_seen"] <= 15
            for node in active_nodes.values()
        )
    if not trusted_node:
        raise PermissionError("Game Node chưa đăng ký heartbeat với Master.")

    payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    signature_payload = f"{timestamp}.{payload_json}".encode("utf-8")
    expected_signature = hmac.new(
        INTERNAL_SERVER_TOKEN.encode("utf-8"),
        signature_payload,
        hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(signature, expected_signature):
        raise PermissionError("Chữ ký yêu cầu Game Node không hợp lệ.")

    match_id = payload.get("match_id")
    player_uids = payload.get("player_uids")
    message = payload.get("message")
    if (
        not isinstance(match_id, str)
        or not match_id
        or len(match_id) > 64
        or not isinstance(player_uids, list)
        or not 2 <= len(player_uids) <= 4
        or any(
            not isinstance(uid, str) or not uid or len(uid) > 256
            for uid in player_uids
        )
        or len(set(player_uids)) != len(player_uids)
        or not isinstance(message, dict)
    ):
        raise ValueError("Thông tin chat trận không hợp lệ.")

    sender_uid = message.get("sender_uid")
    text = message.get("message")
    message_id = message.get("message_id")
    created_at = message.get("created_at")
    if (
        sender_uid not in player_uids
        or not isinstance(text, str)
        or len(text) > 500
        or not isinstance(message_id, str)
        or not message_id
        or len(message_id) > 64
        or not isinstance(created_at, str)
        or len(created_at) > 64
    ):
        raise ValueError("Nội dung chat trận không hợp lệ.")

    attachment = _validate_chat_attachment(
        sender_uid,
        message.get("attachment")
    )
    if not text.strip() and not attachment:
        raise ValueError("Tin nhắn chat phải có nội dung hoặc tệp đính kèm.")

    stored_message = {
        "message_id": message_id,
        "match_id": match_id,
        "sender_uid": sender_uid,
        "sender": message.get("sender", "Người chơi"),
        "message": text.strip(),
        "created_at": created_at
    }
    if attachment:
        stored_message["attachment"] = attachment
    save_match_chat_message_for_players(
        match_id,
        stored_message,
        player_uids
    )
    send_json_message(client_socket, {"status": "success"})


def _public_game_record(record):
    public_record = {
        key: value
        for key, value in record.items()
        if key != "players"
    }
    public_record["players"] = [
        {
            key: value
            for key, value in player.items()
            if key != "uid"
        }
        for player in record["players"]
    ]
    return public_record


def _cloudinary_signature(uid, resource_type):
    if not CLOUDINARY_API_KEY or not CLOUDINARY_API_SECRET or not CLOUDINARY_UPLOAD_PRESET:
        raise RuntimeError(
            "Master cần CLOUDINARY_API_KEY, CLOUDINARY_API_SECRET và "
            "CLOUDINARY_UPLOAD_PRESET."
        )
    if resource_type not in ("image", "video", "raw"):
        raise ValueError("Loại tài nguyên Cloudinary không hợp lệ.")

    now = time.time()
    with cloudinary_signatures_lock:
        recent_signatures = cloudinary_signatures[uid]
        while recent_signatures and now - recent_signatures[0] >= 60:
            recent_signatures.popleft()
        if len(recent_signatures) >= 3:
            raise RuntimeError("Bạn tải lên quá nhanh. Vui lòng đợi một phút.")
        recent_signatures.append(now)

    folder = f"chess_chat/{hashlib.sha256(uid.encode('utf-8')).hexdigest()}"
    params = {
        "overwrite": "false",
        "public_id": f"{folder}/{uuid.uuid4().hex}",
        "timestamp": str(int(now)),
        "upload_preset": CLOUDINARY_UPLOAD_PRESET
    }
    to_sign = "&".join(
        f"{key}={value}" for key, value in sorted(params.items())
    ) + CLOUDINARY_API_SECRET
    signature = hashlib.sha1(to_sign.encode("utf-8")).hexdigest()
    return {
        **params,
        "signature": signature,
        "api_key": CLOUDINARY_API_KEY,
        "cloud_name": CLOUDINARY_CLOUD_NAME,
        "resource_type": resource_type,
        "upload_preset": CLOUDINARY_UPLOAD_PRESET
    }


def _validate_chat_attachment(uid, attachment):
    if attachment is None:
        return None
    if not isinstance(attachment, dict):
        raise ValueError("Thông tin tệp đính kèm không hợp lệ.")

    resource_type = attachment.get("resource_type")
    category = attachment.get("category")
    expected_types = {
        "image": "image",
        "video": "video",
        "audio": "video",
        "document": "raw"
    }
    extensions = {
        "image": {"jpg", "jpeg", "png", "gif", "webp"},
        "video": {"mp4", "mov", "webm"},
        "audio": {"wav", "mp3", "m4a", "ogg", "webm"},
        "document": {"pdf", "doc", "docx"}
    }
    filename = attachment.get("filename")
    public_id = attachment.get("public_id")
    secure_url = attachment.get("secure_url")
    byte_count = attachment.get("bytes")
    file_format = attachment.get("format")
    if (
        category not in expected_types
        or resource_type != expected_types.get(category)
        or not isinstance(filename, str)
        or not filename.strip()
        or len(filename) > 255
        or not isinstance(public_id, str)
        or len(public_id) > 512
        or not isinstance(secure_url, str)
        or len(secure_url) > 2048
        or not secure_url.startswith(
            f"https://res.cloudinary.com/{CLOUDINARY_CLOUD_NAME}/"
        )
        or f"/{resource_type}/upload/" not in secure_url
        or not isinstance(byte_count, int)
        or isinstance(byte_count, bool)
        or not 1 <= byte_count <= MAX_CHAT_ATTACHMENT_BYTES
        or not isinstance(file_format, str)
        or file_format.lower() not in extensions.get(category, set())
    ):
        raise ValueError("Tệp không được hỗ trợ hoặc vượt giới hạn 25 MB.")

    expected_folder = f"chess_chat/{hashlib.sha256(uid.encode('utf-8')).hexdigest()}/"
    if not public_id.startswith(expected_folder):
        raise ValueError("Tệp không thuộc chữ ký upload của tài khoản này.")
    return {
        "filename": filename.strip(),
        "category": category,
        "resource_type": resource_type,
        "public_id": public_id,
        "secure_url": secure_url,
        "format": file_format.lower(),
        "bytes": byte_count
    }


def _public_lobby_message(message):
    public_message = {
        key: value
        for key, value in message.items()
        if key not in ("sender_uid", "record")
    }
    record = message.get("record")
    if isinstance(record, dict):
        public_message["record_summary"] = {
            key: value
            for key, value in _public_game_record(record).items()
            if key not in ("moves", "starting_fen", "final_fen")
        }
        public_message["record_summary"]["moves_count"] = len(
            record.get("moves", [])
        )
    return public_message


def _broadcast_lobby_chat(message):
    response = {
        "type": "lobby_chat",
        **_public_lobby_message(message)
    }
    if isinstance(message.get("record"), dict):
        response["record"] = _public_game_record(message["record"])
    with users_lock:
        clients = list(authenticated_users)
    for client_socket in clients:
        try:
            send_json_message(client_socket, response)
        except OSError as exc:
            print(f"[!] Không thể gửi chat sảnh: {exc}")


# ==========================================
# LUỒNG 1: QUẢN LÝ GAME NODE (UDP HEARTBEAT)
# ==========================================
def listen_for_heartbeats():
    udp_server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp_server.bind(('0.0.0.0', MASTER_UDP_PORT))
    print(f"[*] Master đang lắng nghe Node (UDP) tại cổng {MASTER_UDP_PORT}")
    
    while True:
        addr = None
        try:
            data, addr = udp_server.recvfrom(1024)
            info = json.loads(data.decode('utf-8'))
            if not isinstance(info, dict):
                continue
            action = info.get("action")
            if action in ("room_started", "room_player_left"):
                room_token = info.get("room")
                changed = False
                with rooms_lock:
                    room = waiting_rooms.get(room_token)
                    if room and room["node_ip"] == addr[0]:
                        if action == "room_started":
                            del waiting_rooms[room_token]
                            changed = True
                        else:
                            uid = info.get("uid")
                            if uid == room["host_uid"]:
                                del waiting_rooms[room_token]
                                changed = True
                            elif uid in room["joined_uids"]:
                                room["joined_uids"].remove(uid)
                                changed = True
                if changed:
                    broadcast_waiting_rooms()
                continue

            if (
                action != "heartbeat"
                or not isinstance(info.get("game_port"), int)
                or isinstance(info.get("game_port"), bool)
                or not 1 <= info["game_port"] <= 65535
                or not isinstance(info.get("active_rooms"), int)
                or info["active_rooms"] < 0
            ):
                continue
            node_id = f"{addr[0]}:{info['game_port']}"
            with nodes_lock:
                active_nodes[node_id] = {
                    "ip": addr[0],
                    "game_port": info['game_port'],
                    "active_rooms": info['active_rooms'],
                    "last_seen": time.time()
                }
        except (OSError, ValueError, KeyError, TypeError) as exc:
            print(f"[!] Bỏ qua gói UDP không hợp lệ từ {addr}: {exc}")

def check_dead_nodes():
    while True:
        time.sleep(5)
        current_time = time.time()
        with nodes_lock:
            dead_nodes = [n_id for n_id, data in active_nodes.items() if current_time - data["last_seen"] > 15]
            for node in dead_nodes:
                del active_nodes[node]
        removed_rooms = False
        with rooms_lock:
            dead_node_addresses = {
                (node_id.rsplit(":", 1)[0], int(node_id.rsplit(":", 1)[1]))
                for node_id in dead_nodes
            }
            dead_room_tokens = [
                token for token, room in waiting_rooms.items()
                if (room["node_ip"], room["node_port"]) in dead_node_addresses
            ]
            for token in dead_room_tokens:
                del waiting_rooms[token]
                removed_rooms = True
        for node in dead_nodes:
            print(f"[!] Game Node {node} đã sập. Đã gỡ khỏi hệ thống.")
        if removed_rooms:
            broadcast_waiting_rooms()


def broadcast_online_users():
    """Gửi danh sách người online cho toàn bộ Client đang ở trong Sảnh"""
    with users_lock:
        online_list = [
            {
                "username": user_data["profile"]["username"],
                "rank_points": user_data["profile"].get("rank_points", 1000)
            }
            for user_data in authenticated_users.values()
        ]
        client_sockets = list(authenticated_users.keys())

    response = {"action": "update_online_list", "users": online_list}
    for client_sock in client_sockets:
        try:
            send_json_message(client_sock, response)
        except OSError as exc:
            print(f"[!] Lỗi khi gửi danh sách online: {exc}")


def get_waiting_rooms_for_user(uid):
    with rooms_lock:
        return [
            {
                "token": token,
                "host": room["host"],
                "mode": room["mode"],
                "is_host": room["host_uid"] == uid,
                "is_member": uid in room["joined_uids"],
                "can_cancel": (
                    room["host_uid"] == uid and not room["host_handed_off"]
                ),
                "can_join": (
                    len(room["joined_uids"]) < (
                        4 if room["mode"] == "2vs2" else 2
                    )
                    or uid in room["joined_uids"]
                ),
                "players": len(room["joined_uids"]),
                "capacity": 4 if room["mode"] == "2vs2" else 2
            }
            for token, room in waiting_rooms.items()
        ]


def broadcast_waiting_rooms():
    """Gửi danh sách phòng mới nhất cho từng người đang ở sảnh."""
    with users_lock:
        clients = list(authenticated_users.items())

    for client_sock, user_data in clients:
        response = {
            "action": "update_rooms_list",
            "rooms": get_waiting_rooms_for_user(user_data["uid"])
        }
        try:
            send_json_message(client_sock, response)
        except OSError as exc:
            print(f"[!] Lỗi khi gửi danh sách phòng: {exc}")


def get_online_users():
    with users_lock:
        return [
            {
                "username": user_data["profile"]["username"],
                "rank_points": user_data["profile"].get("rank_points", 1000)
            }
            for user_data in authenticated_users.values()
        ]
# ==========================================
# LUỒNG 2: XỬ LÝ CLIENT (TCP)
# ==========================================
def handle_client(client_socket, addr):
    print(f"[+] Client {addr} vừa kết nối.")
    is_authenticated = False
    buffered_data = b''
    
    try:
        while True:
            # Liên tục chờ gói tin từ Client
            request, buffered_data = receive_json_message(client_socket, buffered_data)
            if request is None:
                break # Mất kết nối
            if not isinstance(request, dict):
                send_json_message(client_socket, {
                    "status": "error",
                    "message": "Yêu cầu không đúng định dạng."
                })
                continue
            action = request.get("action")

            if action == "save_game_record":
                try:
                    _save_internal_game_record(client_socket, request, addr)
                except Exception as exc:
                    send_json_message(client_socket, {
                        "status": "error",
                        "message": f"Không thể lưu hồ sơ trận: {exc}"
                    })
                break
            if action == "save_match_chat_message":
                try:
                    _save_internal_match_chat_message(
                        client_socket,
                        request,
                        addr
                    )
                except Exception as exc:
                    send_json_message(client_socket, {
                        "status": "error",
                        "message": f"Không thể lưu chat trận: {exc}"
                    })
                break
            
            # --- 1. YÊU CẦU ĐĂNG NHẬP ---
            if action == "login" and not is_authenticated:
                id_token = request.get("token")
                try:
                    auth_result = verify_client_token(id_token)
                except Exception as exc:
                    send_json_message(client_socket, {
                        "status": "error",
                        "message": f"Không thể xác thực tài khoản: {exc}"
                    })
                    break
                
                if auth_result["is_valid"]:
                    uid = auth_result["uid"]
                    email = auth_result["email"]
                    user_profile = get_or_create_user_profile(uid, email)
                    
                    with users_lock:
                        authenticated_users[client_socket] = {
                            "uid": uid,
                            "profile": user_profile
                        }
                    is_authenticated = True
                    print(f"[*] {user_profile['username']} đã vào Sảnh!")
                    
                    response = {
                        "status": "success",
                        "message": "Xác thực thành công",
                        "user_data": user_profile,
                        "users": get_online_users(),
                        "rooms": get_waiting_rooms_for_user(uid)
                    }
                    send_json_message(client_socket, response)
                    broadcast_online_users()
                    broadcast_waiting_rooms()
                else:
                    response = {"status": "error", "message": "Xác thực thất bại."}
                    send_json_message(client_socket, response)
                    break # Bật ra nếu token sai
            
            # --- 2. CÁC YÊU CẦU SAU KHI ĐĂNG NHẬP ---
            elif is_authenticated:
                if action == "get_online_users":
                    response = {"action": "update_online_list", "users": get_online_users()}
                    send_json_message(client_socket, response)

                elif action == "get_match_history":
                    with users_lock:
                        user_data = authenticated_users.get(client_socket)
                    if not user_data:
                        break
                    try:
                        records = get_game_records(user_data["uid"])
                        send_json_message(client_socket, {
                            "action": "match_history",
                            "records": [
                                _public_game_record(record)
                                for record in records
                            ]
                        })
                    except Exception as exc:
                        send_json_message(client_socket, {
                            "status": "error",
                            "message": f"Không thể tải hồ sơ trận: {exc}"
                        })

                elif action == "get_lobby_chat_history":
                    try:
                        send_json_message(client_socket, {
                            "action": "lobby_chat_history",
                            "messages": [
                                _public_lobby_message(message)
                                for message in get_lobby_chat_messages()
                            ]
                        })
                    except Exception as exc:
                        send_json_message(client_socket, {
                            "status": "error",
                            "message": f"Không thể tải lịch sử chat sảnh: {exc}"
                        })

                elif action == "get_match_chat":
                    match_id = request.get("match_id")
                    if (
                        not isinstance(match_id, str)
                        or not match_id
                        or len(match_id) > 64
                    ):
                        send_json_message(client_socket, {
                            "status": "error",
                            "message": "Mã trận không hợp lệ."
                        })
                        continue
                    with users_lock:
                        user_data = authenticated_users.get(client_socket)
                    if not user_data:
                        break
                    try:
                        messages = get_match_chat_messages(
                            user_data["uid"],
                            match_id
                        )
                        if messages is None:
                            send_json_message(client_socket, {
                                "status": "error",
                                "message": "Không tìm thấy trận trong hồ sơ của bạn."
                            })
                            continue
                        send_json_message(client_socket, {
                            "action": "match_chat_history",
                            "match_id": match_id,
                            "messages": [
                                _public_lobby_message(message)
                                for message in messages
                            ]
                        })
                    except Exception as exc:
                        send_json_message(client_socket, {
                            "status": "error",
                            "message": f"Không thể tải chat trận đấu: {exc}"
                        })

                elif action == "get_shared_match_record":
                    match_id = request.get("match_id")
                    if (
                        not isinstance(match_id, str)
                        or not match_id
                        or len(match_id) > 64
                    ):
                        send_json_message(client_socket, {
                            "status": "error",
                            "message": "Mã trận không hợp lệ."
                        })
                        continue
                    try:
                        record = get_shared_game_record(match_id)
                        if not record:
                            send_json_message(client_socket, {
                                "status": "error",
                                "message": "Không tìm thấy biên bản trận đã chia sẻ."
                            })
                            continue
                        send_json_message(client_socket, {
                            "action": "shared_match_record",
                            "record": _public_game_record(record)
                        })
                    except Exception as exc:
                        send_json_message(client_socket, {
                            "status": "error",
                            "message": f"Không thể tải biên bản trận: {exc}"
                        })

                elif action == "cloudinary_signature":
                    resource_type = request.get("resource_type")
                    with users_lock:
                        user_data = authenticated_users.get(client_socket)
                    if not user_data:
                        break
                    try:
                        signature = _cloudinary_signature(
                            user_data["uid"],
                            resource_type
                        )
                        signature["request_id"] = request.get("request_id")
                        send_json_message(client_socket, {
                            "action": "cloudinary_signature",
                            **signature
                        })
                    except (RuntimeError, ValueError) as exc:
                        send_json_message(client_socket, {
                            "status": "error",
                            "request_id": request.get("request_id"),
                            "message": str(exc)
                        })

                elif action == "lobby_chat":
                    text = request.get("message", "")
                    match_id = request.get("match_id")
                    attachment = request.get("attachment")
                    if (
                        not isinstance(text, str)
                        or len(text.strip()) > 500
                        or (
                            not text.strip()
                            and not match_id
                            and not attachment
                        )
                        or (
                            match_id is not None
                            and (
                                not isinstance(match_id, str)
                                or not match_id
                                or len(match_id) > 64
                            )
                        )
                    ):
                        send_json_message(client_socket, {
                            "status": "error",
                            "message": "Tin nhắn sảnh phải có nội dung và tối đa 500 ký tự."
                        })
                        continue

                    with users_lock:
                        user_data = authenticated_users.get(client_socket)
                    if not user_data:
                        break

                    try:
                        shared_record = None
                        match_player_uids = ()
                        if match_id:
                            stored_record = get_game_record(
                                user_data["uid"],
                                match_id
                            )
                            if not stored_record:
                                send_json_message(client_socket, {
                                    "status": "error",
                                    "message": "Không tìm thấy trận trong hồ sơ của bạn."
                                })
                                continue
                            if user_data["uid"] not in {
                                player["uid"]
                                for player in stored_record.get("players", [])
                            }:
                                send_json_message(client_socket, {
                                    "status": "error",
                                    "message": "Bạn không có quyền chia sẻ trận này."
                                })
                                continue
                            shared_record = _public_game_record(stored_record)
                            match_player_uids = tuple(
                                player["uid"]
                                for player in stored_record["players"]
                            )

                        validated_attachment = _validate_chat_attachment(
                            user_data["uid"],
                            attachment
                        )
                        chat_message = {
                            "message_id": uuid.uuid4().hex,
                            "sender_uid": user_data["uid"],
                            "sender": user_data["profile"]["username"],
                            "message": text.strip(),
                            "created_at": time.strftime(
                                "%Y-%m-%dT%H:%M:%SZ",
                                time.gmtime()
                            )
                        }
                        if match_id:
                            chat_message["match_id"] = match_id
                            chat_message["record_summary"] = {
                                key: value
                                for key, value in shared_record.items()
                                if key not in (
                                    "moves",
                                    "starting_fen",
                                    "final_fen"
                                )
                            }
                            chat_message["record_summary"]["moves_count"] = len(
                                shared_record.get("moves", [])
                            )
                            chat_message["record"] = shared_record
                        if validated_attachment:
                            chat_message["attachment"] = validated_attachment

                        stored_message = save_lobby_chat_message(
                            chat_message,
                            match_player_uids
                        )
                        _broadcast_lobby_chat({
                            **stored_message,
                            "record": shared_record
                        })
                    except Exception as exc:
                        send_json_message(client_socket, {
                            "status": "error",
                            "message": f"Không thể gửi chat sảnh: {exc}"
                        })
                
                elif action == "create_room":
                    mode = request.get("mode", "1vs1")
                    if mode not in ("1vs1", "2vs2"):
                        send_json_message(client_socket, {"status": "error", "message": "Chế độ phòng không hợp lệ."})
                        continue

                    with users_lock:
                        user_data = authenticated_users.get(client_socket)
                    if not user_data:
                        break
                    uid = user_data["uid"]
                    host_name = user_data["profile"]["username"]

                    with rooms_lock:
                        if any(room["host_uid"] == uid for room in waiting_rooms.values()):
                            send_json_message(client_socket, {
                                "status": "error",
                                "message": "Bạn đã có phòng đang chờ. Hãy hủy phòng đó trước."
                            })
                            continue

                        with nodes_lock:
                            if not active_nodes:
                                send_json_message(client_socket, {
                                    "status": "error",
                                    "message": "Không có Game Node khả dụng. Hãy thử lại sau."
                                })
                                continue
                            best_node_id = min(
                                active_nodes,
                                key=lambda node_id: active_nodes[node_id]["active_rooms"]
                            )
                            best_node = active_nodes[best_node_id].copy()

                        match_token = uuid.uuid4().hex[:8]
                        waiting_rooms[match_token] = {
                            "host_uid": uid,
                            "host": host_name,
                            "mode": mode,
                            "node_ip": best_node["ip"],
                            "node_port": best_node["game_port"],
                            "joined_uids": [uid],
                            "host_handed_off": False
                        }
                    print(f"[*] {host_name} đã tạo phòng {match_token} ({mode}).")
                    broadcast_waiting_rooms()
                elif action == "cancel_room":
                    token = request.get("token")
                    with users_lock:
                        user_data = authenticated_users.get(client_socket)
                    with rooms_lock:
                        room = waiting_rooms.get(token)
                        if not room:
                            send_json_message(client_socket, {
                                "status": "error",
                                "message": "Phòng không còn tồn tại."
                            })
                            continue
                        if not user_data or room["host_uid"] != user_data["uid"]:
                            send_json_message(client_socket, {
                                "status": "error",
                                "message": "Chỉ chủ phòng mới có thể hủy phòng này."
                            })
                            continue
                        if room["host_handed_off"]:
                            send_json_message(client_socket, {
                                "status": "error",
                                "message": "Không thể hủy phòng sau khi trận đấu đã bắt đầu."
                            })
                            continue
                        del waiting_rooms[token]
                    print(f"[*] {user_data['profile']['username']} đã hủy phòng {token}.")
                    broadcast_waiting_rooms()
                elif action == "join_room_request":
                    target_token = request.get("token")
                    with users_lock:
                        user_data = authenticated_users.get(client_socket)
                    with rooms_lock:
                        room_data = waiting_rooms.get(target_token)
                        host_socket = None
                        if not user_data:
                            room_data = None
                        elif room_data:
                            capacity = 4 if room_data["mode"] == "2vs2" else 2
                            is_rejoining = user_data["uid"] in room_data["joined_uids"]
                            if is_rejoining:
                                seat = room_data["joined_uids"].index(user_data["uid"])
                                if seat == 0 and not room_data["host_handed_off"]:
                                    room_data = None
                            elif len(room_data["joined_uids"]) >= capacity:
                                room_data = None
                            else:
                                seat = len(room_data["joined_uids"])
                                room_data["joined_uids"].append(user_data["uid"])

                            if room_data and not room_data["host_handed_off"]:
                                with users_lock:
                                    host_socket = next(
                                        (
                                            sock for sock, user in authenticated_users.items()
                                            if user["uid"] == room_data["host_uid"]
                                        ),
                                        None
                                    )
                                if host_socket:
                                    room_data["host_handed_off"] = True
                                else:
                                    if not is_rejoining:
                                        room_data["joined_uids"].pop()
                                    room_data = None

                    if room_data:
                        broadcast_waiting_rooms()
                        handoff_response = {
                            "action": "handoff",
                            "node_ip": room_data["node_ip"],
                            "node_port": room_data["node_port"],
                            "token": target_token,
                            "mode": room_data["mode"],
                            "capacity": 4 if room_data["mode"] == "2vs2" else 2
                        }
                        if host_socket:
                            host_response = dict(handoff_response)
                            host_response.update({
                                "seat": 0,
                                "username": room_data["host"],
                                "uid": room_data["host_uid"],
                                "is_host": True
                            })
                            send_json_message(host_socket, host_response)
                        guest_response = dict(handoff_response)
                        guest_response.update({
                            "seat": seat,
                            "username": user_data["profile"]["username"],
                            "uid": user_data["uid"],
                            "is_host": False
                        })
                        send_json_message(client_socket, guest_response)
                        print(f"[*] Điều hướng người chơi tới phòng {target_token} (chỗ {seat + 1}).")
                        break

                    send_json_message(client_socket, {
                        "status": "error",
                        "message": "Phòng không tồn tại, đã đủ người hoặc bạn đã tham gia phòng này."
                    })
            else:
                send_json_message(client_socket, {
                    "status": "error",
                    "message": "Vui lòng đăng nhập trước khi thực hiện yêu cầu."
                })
                    
    except Exception as e:
        print(f"[!] Lỗi kết nối với {addr}: {e}")
        
    finally:
        # XÓA USER KHI NGẮT KẾT NỐI 
        with users_lock:
            user_data = authenticated_users.pop(client_socket, None)
        if user_data:
            username = user_data["profile"]["username"]
            print(f"[-] {username} đã thoát khỏi Sảnh.")
            with rooms_lock:
                owned_tokens = [
                    token for token, room in waiting_rooms.items()
                    if room["host_uid"] == user_data["uid"] and not room["host_handed_off"]
                ]
                for token in owned_tokens:
                    del waiting_rooms[token]
            broadcast_online_users()
            if owned_tokens:
                broadcast_waiting_rooms()
        client_socket.close()

# ==========================================
# KHỞI ĐỘNG HỆ THỐNG
# ==========================================
def start_master_server():
    # 1. Bật luồng Node Manager
    threading.Thread(target=listen_for_heartbeats, daemon=True).start()
    threading.Thread(target=check_dead_nodes, daemon=True).start()
    
    # 2. Bật TCP Server cho Client
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(('0.0.0.0', MASTER_TCP_PORT))
    server.listen(100)
    print(f"[*] Master Server (TCP) đang chạy tại cổng {MASTER_TCP_PORT}...")
    
    while True:
        client_socket, addr = server.accept()
        # Cấp 1 luồng riêng cho mỗi Client
        threading.Thread(target=handle_client, args=(client_socket, addr), daemon=True).start()

if __name__ == "__main__":
    start_master_server()