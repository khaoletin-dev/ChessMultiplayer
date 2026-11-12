import base64
import socket
import threading
import json
import time
import secrets
import os
import hashlib
import hmac
import struct
import uuid
from datetime import datetime, timezone
import chess

if __package__:
    from .env_config import load_workspace_env
else:
    from env_config import load_workspace_env

load_workspace_env()

# ==========================================
# CẤU HÌNH NODE
# ==========================================
MASTER_IP = os.getenv("MASTER_SERVER_HOST", "127.0.0.1")
MASTER_TCP_PORT = int(os.getenv("MASTER_TCP_PORT", "8000"))
MASTER_UDP_PORT = int(os.getenv("MASTER_UDP_PORT", "9000"))
INTERNAL_SERVER_TOKEN = os.getenv("CHESS_INTERNAL_SERVER_TOKEN", "")
MY_IP = '0.0.0.0'
MY_TCP_PORT = int(os.getenv("GAME_TCP_PORT", "8001"))
MY_UDP_PORT = int(os.getenv("GAME_UDP_PORT", "8002"))
VOICE_PACKET_PREFIX = b"CV1"
CALL_AUDIO_PACKET_PREFIX = b"CV2"
CALL_VIDEO_PACKET_PREFIX = b"CV3"
MAX_VOICE_PACKET_BYTES = 1200
VIDEO_CHUNK_HEADER = struct.Struct("!IHH")
MAX_VIDEO_CHUNKS = 60
MAX_AUDIO_BYTES = 320044
MAX_AUDIO_BASE64_LENGTH = ((MAX_AUDIO_BYTES + 2) // 3) * 4
MAX_MESSAGE_BYTES = 512 * 1024
CLOUDINARY_CLOUD_NAME = os.getenv("CLOUDINARY_CLOUD_NAME", "dyfmgr6vl")
MAX_CHAT_ATTACHMENT_BYTES = 25 * 1024 * 1024

# Dictionary quản lý các phòng đang chạy: { "token_abc": GameRoom_Object }
active_rooms = {}
active_rooms_lock = threading.RLock()
voice_clients = {}
voice_addresses = {}
voice_clients_lock = threading.Lock()


def send_json_message(client_socket, message):
    payload = json.dumps(message).encode('utf-8') + b'\n'
    client_socket.sendall(payload)


def notify_master_room_event(action, room_id, uid=None):
    event = {"action": action, "room": room_id}
    if uid:
        event["uid"] = uid
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as event_socket:
            event_socket.sendto(
                json.dumps(event).encode("utf-8"),
                (MASTER_IP, MASTER_UDP_PORT)
            )
    except OSError as exc:
        print(f"[!] Không thể đồng bộ trạng thái phòng với Master: {exc}")


def send_game_record_to_master(record):
    if not INTERNAL_SERVER_TOKEN:
        raise RuntimeError(
            "Chưa cấu hình CHESS_INTERNAL_SERVER_TOKEN cho Game Node."
        )

    record_json = json.dumps(record, sort_keys=True, separators=(",", ":"))
    timestamp = int(time.time())
    signature_payload = f"{timestamp}.{record_json}".encode("utf-8")
    signature = hmac.new(
        INTERNAL_SERVER_TOKEN.encode("utf-8"),
        signature_payload,
        hashlib.sha256
    ).hexdigest()
    request = {
        "action": "save_game_record",
        "node_port": MY_TCP_PORT,
        "timestamp": timestamp,
        "signature": signature,
        "record": record
    }
    payload = json.dumps(request).encode("utf-8") + b"\n"

    for attempt in range(3):
        try:
            with socket.create_connection(
                (MASTER_IP, MASTER_TCP_PORT),
                timeout=5
            ) as master_socket:
                master_socket.settimeout(5)
                master_socket.sendall(payload)
                response = b""
                while b"\n" not in response and len(response) <= 4096:
                    chunk = master_socket.recv(1024)
                    if not chunk:
                        break
                    response += chunk
                if b"\n" not in response:
                    raise ConnectionError("Master không phản hồi lưu hồ sơ trận.")
                result = json.loads(response.split(b"\n", 1)[0].decode("utf-8"))
                if result.get("status") != "success":
                    raise RuntimeError(
                        result.get("message", "Master từ chối lưu hồ sơ trận.")
                    )
                return
        except (OSError, ValueError, RuntimeError) as exc:
            if attempt == 2:
                raise RuntimeError(f"Không thể lưu hồ sơ trận lên Master: {exc}") from exc
            time.sleep(1)


def save_match_chat_message_to_master(match_id, player_uids, message):
    if not INTERNAL_SERVER_TOKEN:
        raise RuntimeError(
            "Chưa cấu hình CHESS_INTERNAL_SERVER_TOKEN cho Game Node."
        )

    payload = {
        "match_id": match_id,
        "player_uids": player_uids,
        "message": message
    }
    payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    timestamp = int(time.time())
    signature = hmac.new(
        INTERNAL_SERVER_TOKEN.encode("utf-8"),
        f"{timestamp}.{payload_json}".encode("utf-8"),
        hashlib.sha256
    ).hexdigest()
    request = {
        "action": "save_match_chat_message",
        "node_port": MY_TCP_PORT,
        "timestamp": timestamp,
        "payload": payload,
        "signature": signature
    }
    request_data = json.dumps(request).encode("utf-8") + b"\n"

    for attempt in range(3):
        try:
            with socket.create_connection(
                (MASTER_IP, MASTER_TCP_PORT),
                timeout=5
            ) as master_socket:
                master_socket.settimeout(5)
                master_socket.sendall(request_data)
                response = b""
                while b"\n" not in response and len(response) <= 4096:
                    chunk = master_socket.recv(1024)
                    if not chunk:
                        break
                    response += chunk
                if b"\n" not in response:
                    raise ConnectionError("Master không phản hồi lưu chat trận.")
                result = json.loads(response.split(b"\n", 1)[0].decode("utf-8"))
                if result.get("status") != "success":
                    raise RuntimeError(
                        result.get("message", "Master từ chối lưu chat trận.")
                    )
                return
        except (OSError, ValueError, RuntimeError) as exc:
            if attempt == 2:
                raise RuntimeError(f"Không thể lưu chat trận lên Master: {exc}") from exc
            time.sleep(1)


def _validate_game_attachment(uid, attachment):
    if attachment is None:
        return None
    if not isinstance(attachment, dict):
        raise ValueError("Thông tin tệp đính kèm không hợp lệ.")

    allowed = {
        "image": ("image", {"jpg", "jpeg", "png", "gif", "webp"}),
        "video": ("video", {"mp4", "mov", "webm"}),
        "audio": ("video", {"wav", "mp3", "m4a", "ogg", "webm"}),
        "document": ("raw", {"pdf", "doc", "docx"})
    }
    category = attachment.get("category")
    expected = allowed.get(category)
    if not expected:
        raise ValueError("Loại tệp đính kèm không được hỗ trợ.")
    resource_type, formats = expected
    public_id = attachment.get("public_id")
    secure_url = attachment.get("secure_url")
    filename = attachment.get("filename")
    file_format = attachment.get("format")
    byte_count = attachment.get("bytes")
    if (
        attachment.get("resource_type") != resource_type
        or not isinstance(public_id, str)
        or not isinstance(secure_url, str)
        or not secure_url.startswith(
            f"https://res.cloudinary.com/{CLOUDINARY_CLOUD_NAME}/"
        )
        or f"/{resource_type}/upload/" not in secure_url
        or len(secure_url) > 2048
        or not isinstance(filename, str)
        or not filename.strip()
        or len(filename) > 255
        or not isinstance(file_format, str)
        or file_format.lower() not in formats
        or not isinstance(byte_count, int)
        or isinstance(byte_count, bool)
        or not 1 <= byte_count <= MAX_CHAT_ATTACHMENT_BYTES
    ):
        raise ValueError("Tệp không hợp lệ hoặc vượt giới hạn 25 MB.")

    expected_folder = (
        "chess_chat/"
        f"{hashlib.sha256(uid.encode('utf-8')).hexdigest()}/"
    )
    if (
        not public_id.startswith(expected_folder)
        or f"/{public_id}." not in secure_url
    ):
        raise ValueError("Tệp không thuộc chữ ký upload của người gửi.")
    return {
        "filename": filename.strip(),
        "category": category,
        "resource_type": resource_type,
        "public_id": public_id,
        "secure_url": secure_url,
        "format": file_format.lower(),
        "bytes": byte_count
    }


def receive_json_message(client_socket, buffered_data):
    while b'\n' not in buffered_data:
        chunk = client_socket.recv(1024)
        if not chunk:
            return None, buffered_data
        buffered_data += chunk
        if len(buffered_data) > MAX_MESSAGE_BYTES:
            raise ValueError("Gói tin từ client vượt quá giới hạn cho phép.")

    message_data, buffered_data = buffered_data.split(b'\n', 1)
    if not message_data:
        return receive_json_message(client_socket, buffered_data)
    return json.loads(message_data.decode('utf-8')), buffered_data


# ==========================================
# CLASS QUẢN LÝ PHÒNG GAME
# ==========================================
class GameRoom:
    def __init__(self, room_id, mode):
        self.room_id = room_id
        self.mode = mode
        self.capacity = 4 if mode == "2vs2" else 2
        self.players = {}
        self.participants = {}
        self.moves = []
        self.status = "waiting"
        self.board = chess.Board()
        self.lock = threading.RLock()
        self.started_at = None
        self.match_id = uuid.uuid4().hex
        self.pending_calls = {}
        self.active_calls = {}

    def public_roster(self):
        return [
            {
                "seat": player["seat"],
                "username": player["username"],
                "team": player["team"]
            }
            for player in sorted(self.players.values(), key=lambda item: item["seat"])
        ]

    def _finish_game(self, result, reason):
        if self.status == "game_over":
            return
        self.status = "game_over"
        for call_id in list(self.active_calls):
            self.end_call(call_id, "Ván đấu đã kết thúc.", notify_caller=True)
        for call_id, invitation in list(self.pending_calls.items()):
            caller = self.players.get(invitation["caller_seat"])
            target = self.players.get(invitation["target_seat"])
            self._clear_pending_call(call_id)
            for player in (caller, target):
                if player:
                    try:
                        send_json_message(player["socket"], {
                            "action": "call_invite_cancelled",
                            "call_id": call_id
                        })
                    except OSError as exc:
                        print(f"[!] Không thể hủy lời mời gọi: {exc}")
        self.broadcast({
            "action": "game_over",
            "result": result,
            "message": reason,
            "fen": self.board.fen(),
            "match_id": self.match_id
        })
        record = {
            "match_id": self.match_id,
            "room_id": self.room_id,
            "mode": self.mode,
            "result": result,
            "reason": reason,
            "players": [
                {
                    "uid": player["uid"],
                    "username": player["username"],
                    "team": player["team"],
                    "is_leader": player["is_leader"]
                }
                for player in self.participants.values()
            ],
            "moves": list(self.moves),
            "starting_fen": chess.STARTING_FEN,
            "final_fen": self.board.fen(),
            "started_at": self.started_at,
            "completed_at": datetime.now(timezone.utc).isoformat()
        }
        threading.Thread(
            target=self._save_game_record,
            args=(record,),
            daemon=True
        ).start()

    def _save_game_record(self, record):
        try:
            send_game_record_to_master(record)
        except Exception as exc:
            print(f"[!] Không thể lưu hồ sơ trận {record['match_id']}: {exc}")
            self.broadcast({
                "action": "match_record_save_failed",
                "match_id": record["match_id"],
                "message": str(exc)
            })
        else:
            print(f"[DB] Đã lưu hồ sơ trận {record['match_id']} lên Firebase.")
            self.broadcast({
                "action": "match_record_saved",
                "match_id": record["match_id"]
            })

    def send_chat(self, client_socket, text, attachment=None):
        player = self.get_player(client_socket)
        if not player:
            return {"status": "error", "message": "Bạn không thuộc phòng này."}
        if self.status == "game_over":
            return {"status": "error", "message": "Ván cờ đã kết thúc."}
        if (
            not isinstance(text, str)
            or len(text.strip()) > 500
            or (not text.strip() and not attachment)
        ):
            return {
                "status": "error",
                "message": "Tin nhắn cần có nội dung hoặc tệp đính kèm."
            }
        try:
            validated_attachment = _validate_game_attachment(
                player["uid"],
                attachment
            )
        except ValueError as exc:
            return {"status": "error", "message": str(exc)}

        message = {
            "message_id": uuid.uuid4().hex,
            "sender": player["username"],
            "sender_uid": player["uid"],
            "message": text.strip(),
            "created_at": datetime.now(timezone.utc).isoformat()
        }
        if validated_attachment:
            message["attachment"] = validated_attachment
        self.broadcast({
            "type": "chat",
            **message
        })
        payload_message = dict(message)
        player_uids = list(
            dict.fromkeys(participant["uid"] for participant in self.participants.values())
        )
        threading.Thread(
            target=self._save_match_chat_message,
            args=(player_uids, payload_message),
            daemon=True
        ).start()
        return None

    def _save_match_chat_message(self, player_uids, message):
        try:
            save_match_chat_message_to_master(
                self.match_id,
                player_uids,
                message
            )
        except Exception as exc:
            print(
                f"[!] Không thể lưu chat trận {self.match_id} "
                f"({message['message_id']}): {exc}"
            )

    def _get_outcome_message(self, outcome):
        termination_messages = {
            chess.Termination.CHECKMATE: "Chiếu bí",
            chess.Termination.STALEMATE: "Hòa do hết nước đi",
            chess.Termination.INSUFFICIENT_MATERIAL: "Hòa do không đủ quân chiếu bí",
            chess.Termination.SEVENTYFIVE_MOVES: "Hòa do luật 75 nước",
            chess.Termination.FIVEFOLD_REPETITION: "Hòa do lặp lại thế cờ 5 lần"
        }
        reason = termination_messages.get(outcome.termination, "Ván cờ kết thúc")
        if outcome.winner is None:
            return f"{reason}."
        winner = "Trắng" if outcome.winner else "Đen"
        return f"{reason}. {winner} thắng!"

    def can_add_player(self, seat):
        return (
            isinstance(seat, int)
            and 0 <= seat < self.capacity
            and seat not in self.players
            and self.status == "waiting"
        )

    def add_player(self, client_socket, seat, username, uid, voice_token):
        if not self.can_add_player(seat):
            return False

        self.players[seat] = {
            "socket": client_socket,
            "seat": seat,
            "username": username,
            "uid": uid,
            "team": "white" if seat < self.capacity // 2 else "black",
            "is_leader": self.mode != "2vs2" or seat in (0, 2),
            "voice_token": voice_token
        }
        self.participants[seat] = {
            key: value
            for key, value in self.players[seat].items()
            if key != "socket" and key != "voice_token"
        }
        print(f"[Room {self.room_id}] {username} đã vào chỗ {seat + 1}.")
        self.broadcast({
            "action": "room_state",
            "mode": self.mode,
            "status": self.status,
            "players": len(self.players),
            "capacity": self.capacity,
            "roster": self.public_roster(),
            "fen": self.board.fen(),
            "turn": "white" if self.board.turn else "black"
        })

        if len(self.players) == self.capacity:
            self.status = "playing"
            self.started_at = datetime.now(timezone.utc).isoformat()
            self.broadcast({
                "action": "game_start",
                "message": "Đủ người chơi. Trận đấu bắt đầu!",
                "mode": self.mode,
                "players": len(self.players),
                "capacity": self.capacity,
                "roster": self.public_roster(),
                "fen": self.board.fen(),
                "turn": "white" if self.board.turn else "black"
            })
            notify_master_room_event("room_started", self.room_id)
        return True

    def get_player(self, client_socket):
        return next(
            (player for player in self.players.values() if player["socket"] is client_socket),
            None
        )

    def invite_call(self, client_socket, target_seat):
        caller = self.get_player(client_socket)
        target = self.players.get(target_seat)
        if (
            not caller
            or not isinstance(target_seat, int)
            or isinstance(target_seat, bool)
            or not target
            or target is caller
        ):
            return {"status": "error", "message": "Người nhận lời mời không hợp lệ."}
        if self.status != "playing":
            return {"status": "error", "message": "Cuộc gọi chỉ dùng khi ván cờ đang diễn ra."}
        if caller.get("active_call_id") or caller.get("pending_call_id"):
            return {"status": "error", "message": "Bạn đang có cuộc gọi hoặc lời mời chờ xử lý."}
        if target.get("active_call_id") or target.get("pending_call_id"):
            return {"status": "error", "message": "Người chơi này đang bận trong một cuộc gọi khác."}

        call_id = uuid.uuid4().hex
        self.pending_calls[call_id] = {
            "caller_seat": caller["seat"],
            "target_seat": target["seat"],
            "timer": None
        }
        caller["pending_call_id"] = call_id
        target["pending_call_id"] = call_id
        timer = threading.Timer(30, self._expire_call_invite, args=(call_id,))
        timer.daemon = True
        self.pending_calls[call_id]["timer"] = timer
        timer.start()
        try:
            send_json_message(target["socket"], {
                "action": "call_invite",
                "call_id": call_id,
                "caller_seat": caller["seat"],
                "caller_name": caller["username"]
            })
            send_json_message(caller["socket"], {
                "action": "call_invite_sent",
                "call_id": call_id,
                "target_name": target["username"]
            })
        except OSError as exc:
            self._clear_pending_call(call_id)
            return {"status": "error", "message": f"Không thể gửi lời mời gọi: {exc}"}
        return None

    def respond_to_call(self, client_socket, call_id, accepted):
        responder = self.get_player(client_socket)
        invitation = self.pending_calls.get(call_id)
        if (
            not responder
            or not isinstance(call_id, str)
            or not isinstance(accepted, bool)
            or not invitation
            or invitation["target_seat"] != responder["seat"]
        ):
            return {"status": "error", "message": "Lời mời gọi không còn hợp lệ."}

        caller = self.players.get(invitation["caller_seat"])
        self._clear_pending_call(call_id)
        if not caller:
            return {"status": "error", "message": "Người gọi đã rời phòng."}
        if not accepted:
            try:
                send_json_message(caller["socket"], {
                    "action": "call_rejected",
                    "call_id": call_id,
                    "target_name": responder["username"]
                })
            except OSError as exc:
                print(f"[!] Không thể báo từ chối lời mời gọi: {exc}")
            return None
        if self.status != "playing" or caller.get("active_call_id"):
            return {"status": "error", "message": "Người gọi không còn sẵn sàng."}

        self.active_calls[call_id] = (caller["seat"], responder["seat"])
        caller["active_call_id"] = call_id
        responder["active_call_id"] = call_id
        for player, peer in ((caller, responder), (responder, caller)):
            try:
                send_json_message(player["socket"], {
                    "action": "call_started",
                    "call_id": call_id,
                    "peer": {
                        "seat": peer["seat"],
                        "username": peer["username"]
                    }
                })
            except OSError as exc:
                print(f"[!] Không thể bắt đầu cuộc gọi cho người chơi: {exc}")
        return None

    def _clear_pending_call(self, call_id):
        invitation = self.pending_calls.pop(call_id, None)
        if not invitation:
            return None
        timer = invitation.get("timer")
        if timer:
            timer.cancel()
        for seat in (invitation["caller_seat"], invitation["target_seat"]):
            player = self.players.get(seat)
            if player and player.get("pending_call_id") == call_id:
                player["pending_call_id"] = None
        return invitation

    def _expire_call_invite(self, call_id):
        with self.lock:
            invitation = self._clear_pending_call(call_id)
            if not invitation:
                return
            caller = self.players.get(invitation["caller_seat"])
            target = self.players.get(invitation["target_seat"])
            if caller:
                try:
                    send_json_message(caller["socket"], {
                        "action": "call_invite_timeout",
                        "call_id": call_id
                    })
                except OSError as exc:
                    print(f"[!] Không thể báo hết hạn lời mời gọi: {exc}")
            if target:
                try:
                    send_json_message(target["socket"], {
                        "action": "call_invite_cancelled",
                        "call_id": call_id
                    })
                except OSError as exc:
                    print(f"[!] Không thể hủy lời mời gọi hết hạn: {exc}")

    def end_call(self, call_id, reason="Cuộc gọi đã kết thúc.", notify_caller=False):
        seats = self.active_calls.pop(call_id, None)
        if not seats:
            return
        for seat in seats:
            player = self.players.get(seat)
            if not player:
                continue
            player["active_call_id"] = None
            if notify_caller:
                try:
                    send_json_message(player["socket"], {
                        "action": "call_ended",
                        "call_id": call_id,
                        "message": reason
                    })
                except OSError as exc:
                    print(f"[!] Không thể kết thúc cuộc gọi: {exc}")

    def end_call_for_player(self, client_socket, reason):
        player = self.get_player(client_socket)
        if not player:
            return
        call_id = player.get("active_call_id")
        if call_id:
            self.end_call(call_id, reason, notify_caller=True)
            return
        call_id = player.get("pending_call_id")
        if not call_id:
            return
        invitation = self._clear_pending_call(call_id)
        if invitation and invitation["caller_seat"] == player["seat"]:
            peer = self.players.get(invitation["target_seat"])
            if peer:
                try:
                    send_json_message(peer["socket"], {
                        "action": "call_invite_cancelled",
                        "call_id": call_id
                    })
                except OSError as exc:
                    print(f"[!] Không thể hủy lời mời gọi: {exc}")
        elif invitation:
            caller = self.players.get(invitation["caller_seat"])
            if caller:
                try:
                    send_json_message(caller["socket"], {
                        "action": "call_invite_cancelled",
                        "call_id": call_id
                    })
                except OSError as exc:
                    print(f"[!] Không thể hủy lời mời gọi: {exc}")

    def process_move(self, client_socket, move_uci):
        player = self.get_player(client_socket)
        if not player:
            return {"status": "error", "message": "Bạn không thuộc phòng này."}
        if self.status != "playing":
            return {"status": "error", "message": "Ván cờ chưa bắt đầu hoặc đã kết thúc."}

        current_team = "white" if self.board.turn else "black"
        if player["team"] != current_team:
            return {"status": "invalid", "message": "Chưa đến lượt của đội bạn."}
        if self.mode == "2vs2" and not player["is_leader"]:
            return {"status": "invalid", "message": "Chỉ trưởng nhóm được quyết định nước đi."}
        if not isinstance(move_uci, str):
            return {"status": "error", "message": "Cú pháp nước đi bị lỗi!"}

        try:
            move = chess.Move.from_uci(move_uci)
            if move not in self.board.legal_moves:
                return {"status": "invalid", "message": "Nước đi không hợp lệ!"}

            san = self.board.san(move)
            self.board.push(move)
            self.moves.append({
                "ply": len(self.moves) + 1,
                "uci": move_uci,
                "san": san
            })
            outcome = self.board.outcome()
            is_check = self.board.is_check()
            game_over = outcome is not None
            result = self.board.result() if outcome else None
            self.broadcast({
                "type": "move_result",
                "status": "valid",
                "sender": player["username"],
                "move": move_uci,
                "is_check": is_check,
                "is_checkmate": self.board.is_checkmate(),
                "is_stalemate": self.board.is_stalemate(),
                "fen": self.board.fen(),
                "game_over": game_over,
                "game_result": result
            })

            if outcome:
                self._finish_game(result, self._get_outcome_message(outcome))
        except ValueError:
            return {"status": "error", "message": "Cú pháp nước đi bị lỗi!"}

    def claim_draw(self, client_socket):
        if not self.get_player(client_socket):
            return {"status": "error", "message": "Bạn không thuộc phòng này."}
        if self.status != "playing":
            return {"status": "error", "message": "Không có ván cờ đang diễn ra."}
        if not self.board.can_claim_draw():
            return {"status": "invalid", "message": "Chưa đủ điều kiện tuyên bố hòa."}

        outcome = self.board.outcome(claim_draw=True)
        if outcome is None:
            return {"status": "error", "message": "Không thể xác nhận kết quả hòa."}
        reason = (
            "Hòa do luật 50 nước."
            if outcome.termination == chess.Termination.FIFTY_MOVES
            else "Hòa do lặp lại thế cờ 3 lần."
        )
        self._finish_game("1/2-1/2", reason)

    def resign(self, client_socket):
        player = self.get_player(client_socket)
        if not player:
            return {"status": "error", "message": "Bạn không thuộc phòng này."}
        if self.status != "playing":
            return {"status": "error", "message": "Không có ván cờ đang diễn ra."}

        winner = "Đen" if player["team"] == "white" else "Trắng"
        resigning_team = "Trắng" if player["team"] == "white" else "Đen"
        result = "0-1" if player["team"] == "white" else "1-0"
        self._finish_game(result, f"Đội {resigning_team} đầu hàng. Đội {winner} thắng!")

    def broadcast(self, message):
        for player in self.players.values():
            try:
                send_json_message(player["socket"], message)
            except OSError as exc:
                print(f"[!] Không thể gửi dữ liệu tới người chơi: {exc}")

    def remove_client(self, client_socket):
        seat = next(
            (seat for seat, player in self.players.items() if player["socket"] is client_socket),
            None
        )
        if seat is None:
            return None

        player = self.players.pop(seat)
        if self.status == "playing":
            losing_team = player["team"]
            winner_team = "black" if losing_team == "white" else "white"
            winner = "Đen" if winner_team == "black" else "Trắng"
            result = "0-1" if losing_team == "white" else "1-0"
            self._finish_game(
                result,
                f"{player['username']} đã thoát trận. Đội {winner} thắng do đối thủ rời trận."
            )
        else:
            self.broadcast({
                "action": "player_left",
                "message": f"{player['username']} đã thoát trận."
            })
            self.broadcast({
                "action": "room_state",
                "mode": self.mode,
                "status": self.status,
                "players": len(self.players),
                "capacity": self.capacity,
                "roster": self.public_roster()
            })
        return player["voice_token"], player["uid"]

# ==========================================
# LUỒNG XỬ LÝ CLIENT
# ==========================================
def handle_client(client_socket, addr):
    print(f"[+] Có kết nối mới tới Game Node từ {addr}")
    current_room = None
    voice_token = None
    buffered_data = b''
    
    try:
        request, buffered_data = receive_json_message(client_socket, buffered_data)
        if request is None:
            return
        
        if (
            not isinstance(request, dict)
            or request.get("action") != "join_game"
            or not isinstance(request.get("token"), str)
            or not request["token"]
            or request.get("mode") not in ("1vs1", "2vs2")
            or not isinstance(request.get("seat"), int)
            or isinstance(request.get("seat"), bool)
            or not isinstance(request.get("username"), str)
            or not request["username"].strip()
            or not isinstance(request.get("uid"), str)
            or not request["uid"]
        ):
            send_json_message(client_socket, {
                "status": "error",
                "message": "Thông tin tham gia phòng không hợp lệ."
            })
            return

        token = request["token"]
        mode = request["mode"]
        seat = request["seat"]
        username = request["username"].strip()
        uid = request["uid"]
        voice_token = secrets.token_urlsafe(24)

        with active_rooms_lock:
            current_room = active_rooms.get(token)
            if current_room is None:
                current_room = GameRoom(token, mode)
                active_rooms[token] = current_room

            with current_room.lock:
                available_seat = next(
                    (
                        seat_index for seat_index in range(current_room.capacity)
                        if seat_index not in current_room.players
                    ),
                    None
                )
                if available_seat is not None:
                    seat = available_seat
                if current_room.mode != mode or not current_room.can_add_player(seat):
                    send_json_message(client_socket, {
                        "status": "error",
                        "message": "Phòng đã đủ người hoặc chỗ ngồi không hợp lệ."
                    })
                    if not current_room.players:
                        active_rooms.pop(token, None)
                    current_room = None
                    return

                team = "white" if seat < current_room.capacity // 2 else "black"
                is_leader = mode != "2vs2" or seat in (0, 2)
                roster = current_room.public_roster() + [{
                    "seat": seat,
                    "username": username,
                    "team": team
                }]
                send_json_message(client_socket, {
                    "status": "success",
                    "message": f"Đã kết nối vào phòng {token}",
                    "mode": mode,
                    "seat": seat,
                    "team": team,
                    "is_leader": is_leader,
                    "players": len(current_room.players) + 1,
                    "capacity": current_room.capacity,
                    "roster": sorted(roster, key=lambda player: player["seat"]),
                    "fen": current_room.board.fen(),
                    "turn": "white" if current_room.board.turn else "black",
                    "voice_token": voice_token,
                    "voice_port": MY_UDP_PORT
                })
                current_room.add_player(
                    client_socket,
                    seat,
                    username,
                    uid,
                    voice_token
                )

        if voice_token:
            with voice_clients_lock:
                voice_clients[voice_token] = {
                    "room": current_room,
                    "team": team,
                    "seat": seat,
                    "address": None
                }
            
        while True:
            game_msg, buffered_data = receive_json_message(client_socket, buffered_data)
            if game_msg is None:
                break

            if not isinstance(game_msg, dict):
                send_json_message(client_socket, {
                    "status": "error",
                    "message": "Gói tin trò chơi không hợp lệ."
                })
                continue

            if game_msg.get("type") == "chat":
                chat_message = game_msg.get("message")
                with current_room.lock:
                    error_res = current_room.send_chat(
                        client_socket,
                        chat_message,
                        game_msg.get("attachment")
                    )
                    if error_res:
                        send_json_message(client_socket, {
                            **error_res
                        })
            elif game_msg.get("type") == "call_invite":
                with current_room.lock:
                    error_res = current_room.invite_call(
                        client_socket,
                        game_msg.get("target_seat")
                    )
                if error_res:
                    send_json_message(client_socket, error_res)
            elif game_msg.get("type") == "call_response":
                with current_room.lock:
                    error_res = current_room.respond_to_call(
                        client_socket,
                        game_msg.get("call_id"),
                        game_msg.get("accepted")
                    )
                if error_res:
                    send_json_message(client_socket, error_res)
            elif game_msg.get("type") == "call_end":
                with current_room.lock:
                    current_room.end_call_for_player(
                        client_socket,
                        "Người chơi đã kết thúc cuộc gọi."
                    )
            elif game_msg.get("type") == "audio":
                player = current_room.get_player(client_socket)
                audio_data = game_msg.get("audio", "")
                if (
                    not player
                    or game_msg.get("format") != "wav"
                    or not isinstance(audio_data, str)
                    or len(audio_data) > MAX_AUDIO_BASE64_LENGTH
                ):
                    send_json_message(client_socket, {
                        "type": "audio_error",
                        "message": "Tin nhắn thoại không hợp lệ hoặc vượt quá 10 giây."
                    })
                    continue
                try:
                    decoded_audio = base64.b64decode(audio_data, validate=True)
                except (ValueError, base64.binascii.Error):
                    send_json_message(client_socket, {
                        "type": "audio_error",
                        "message": "Dữ liệu ghi âm không hợp lệ."
                    })
                    continue
                if len(decoded_audio) > MAX_AUDIO_BYTES:
                    send_json_message(client_socket, {
                        "type": "audio_error",
                        "message": "Tin nhắn thoại vượt quá giới hạn 10 giây."
                    })
                    continue
                with current_room.lock:
                    current_room.broadcast({
                        "type": "audio",
                        "sender": player["username"],
                        "format": "wav",
                        "audio": audio_data
                    })
            elif game_msg.get("type") == "move":
                move_uci = game_msg.get("move")
                with current_room.lock:
                    error_res = current_room.process_move(client_socket, move_uci)
                if error_res:
                    send_json_message(client_socket, error_res)
            elif game_msg.get("type") == "draw_claim":
                with current_room.lock:
                    error_res = current_room.claim_draw(client_socket)
                if error_res:
                    send_json_message(client_socket, error_res)
            elif game_msg.get("type") == "resign":
                with current_room.lock:
                    error_res = current_room.resign(client_socket)
                if error_res:
                    send_json_message(client_socket, error_res)
                    
    except Exception as e:
        print(f"[!] Lỗi Client {addr}: {e}")
        
    finally:
        if current_room:
            with active_rooms_lock:
                with current_room.lock:
                    current_room.end_call_for_player(
                        client_socket,
                        "Người chơi đã rời phòng."
                    )
                    removal = current_room.remove_client(client_socket)
                    removed_voice_token = removal[0] if removal else None
                    removed_uid = removal[1] if removal else None
                    if not current_room.players:
                        active_rooms.pop(current_room.room_id, None)
                        print(f"[-] Đã hủy phòng {current_room.room_id} vì trống.")
                    if current_room.status == "waiting" and removed_uid:
                        notify_master_room_event(
                            "room_player_left",
                            current_room.room_id,
                            removed_uid
                        )
            if removed_voice_token:
                with voice_clients_lock:
                    voice_client = voice_clients.pop(removed_voice_token, None)
                    if voice_client and voice_client["address"]:
                        voice_addresses.pop(voice_client["address"], None)
        client_socket.close()


def handle_voice_datagram(udp_socket, data, addr):
    if data.startswith((
        VOICE_PACKET_PREFIX,
        CALL_AUDIO_PACKET_PREFIX,
        CALL_VIDEO_PACKET_PREFIX
    )):
        if len(data) <= len(VOICE_PACKET_PREFIX) or len(data) > MAX_VOICE_PACKET_BYTES:
            return
        prefix = next(
            packet_prefix
            for packet_prefix in (
                VOICE_PACKET_PREFIX,
                CALL_AUDIO_PACKET_PREFIX,
                CALL_VIDEO_PACKET_PREFIX
            )
            if data.startswith(packet_prefix)
        )
        if prefix == CALL_AUDIO_PACKET_PREFIX:
            audio_data = data[len(prefix):]
            if not audio_data or len(audio_data) % 2:
                return
        elif prefix == CALL_VIDEO_PACKET_PREFIX:
            header_end = len(prefix) + VIDEO_CHUNK_HEADER.size
            if len(data) <= header_end:
                return
            _frame_id, chunk_index, chunk_count = VIDEO_CHUNK_HEADER.unpack(
                data[len(prefix):header_end]
            )
            if (
                not 1 <= chunk_count <= MAX_VIDEO_CHUNKS
                or chunk_index >= chunk_count
            ):
                return

        with voice_clients_lock:
            token = voice_addresses.get(addr)
            sender = voice_clients.get(token)
            if not sender:
                return
            room = sender["room"]
            team = sender["team"]
            sender_seat = sender["seat"]
        with room.lock:
            if room.status != "playing":
                return
            if prefix == VOICE_PACKET_PREFIX:
                destination_tokens = [
                    player["voice_token"]
                    for player in room.players.values()
                    if player["team"] == team
                    and player["seat"] != sender_seat
                ]
            else:
                player = room.players.get(sender_seat)
                call_id = player.get("active_call_id") if player else None
                call_seats = room.active_calls.get(call_id)
                if not call_seats or sender_seat not in call_seats:
                    return
                peer_seat = next(seat for seat in call_seats if seat != sender_seat)
                peer = room.players.get(peer_seat)
                destination_tokens = [peer["voice_token"]] if peer else []
        with voice_clients_lock:
            destinations = [
                voice_clients[peer_token]["address"]
                for peer_token in destination_tokens
                if peer_token in voice_clients
                and voice_clients[peer_token]["room"] is room
                and voice_clients[peer_token]["address"] is not None
            ]
        for destination in destinations:
            try:
                udp_socket.sendto(data, destination)
            except OSError as exc:
                print(f"[!] Không thể chuyển tiếp gói voice UDP: {exc}")
        return

    try:
        message = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return

    if not isinstance(message, dict) or message.get("type") != "register":
        return
    token = message.get("token")
    if not isinstance(token, str):
        return

    with voice_clients_lock:
        voice_client = voice_clients.get(token)
        if not voice_client:
            return
        old_address = voice_client["address"]
        if old_address:
            voice_addresses.pop(old_address, None)
        voice_client["address"] = addr
        voice_addresses[addr] = token
    try:
        udp_socket.sendto(b'{"type":"registered"}', addr)
    except OSError as exc:
        print(f"[!] Không thể xác nhận đăng ký voice UDP: {exc}")


def serve_udp_voice(udp_socket):
    print(f"[*] Game Node đang nhận voice UDP tại cổng {MY_UDP_PORT}.")
    while True:
        try:
            data, addr = udp_socket.recvfrom(MAX_VOICE_PACKET_BYTES + 1)
            handle_voice_datagram(udp_socket, data, addr)
        except OSError as exc:
            print(f"[!] Lỗi socket voice UDP: {exc}")
            break

# ==========================================
# LUỒNG NHỊP TIM UDP BÁO VỀ MASTER
# ==========================================
def send_heartbeat():
    udp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    while True:
        heartbeat_data = {
            "action": "heartbeat",
            "game_port": MY_TCP_PORT,
            "active_rooms": len(active_rooms) # Báo cáo số phòng thực tế đang chạy
        }
        try:
            udp_socket.sendto(json.dumps(heartbeat_data).encode('utf-8'), (MASTER_IP, MASTER_UDP_PORT))
        except:
            pass
        time.sleep(5)

# ==========================================
# KHỞI ĐỘNG GAME NODE
# ==========================================
if __name__ == "__main__":
    # Chạy luồng báo cáo nhịp tim
    threading.Thread(target=send_heartbeat, daemon=True).start()

    udp_voice_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp_voice_socket.bind((MY_IP, MY_UDP_PORT))
    threading.Thread(
        target=serve_udp_voice,
        args=(udp_voice_socket,),
        daemon=True
    ).start()
    
    # Khởi động TCP Server đón người chơi
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind((MY_IP, MY_TCP_PORT))
    server.listen(50)
    print(f"[*] Game Node đang chạy tại {MY_IP}:{MY_TCP_PORT}...")
    
    while True:
        client, address = server.accept()
        threading.Thread(target=handle_client, args=(client, address), daemon=True).start()