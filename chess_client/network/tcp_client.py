import os
import socket
import json
import threading
import uuid

MAX_MESSAGE_BYTES = 512 * 1024

class ChessTCPClient:
    def __init__(self):
        self.host = os.getenv("CHESS_SERVER_HOST", "127.0.0.1")
        self.port = int(os.getenv("CHESS_SERVER_PORT", "8000"))
        self.client_socket = None
        self.is_connected = False
        self._send_lock = threading.Lock()
        self._listener_thread = None
        self._room_state_lock = threading.Lock()
        self._pending_room_state_messages = []
        self.last_error = None
        self.user_data = {}
        self._auth_token = None
        self.on_online_list_update = None
        self.on_join_game_success = None
        self.on_chat_received = None
        self.on_audio_received = None
        self.on_move_received = None
        self.on_server_error = None
        self.on_room_state_update = None
        self.on_game_over = None
        self.on_match_record_save_status = None
        self.on_call_event = None
        self.online_users = []
        self._receive_buffer = b''
        self.on_rooms_list_update = None
        self.rooms = []
        self.on_match_history_update = None
        self.on_lobby_chat_received = None
        self.on_lobby_chat_history = None
        self.on_match_chat_history = None
        self.on_shared_match_record = None
        self._request_callbacks = {}
        self._request_lock = threading.Lock()

    def set_online_list_callback(self, callback):
        self.on_online_list_update = callback
        if callback:
            callback(self.online_users)

    def set_rooms_list_callback(self, callback):
        self.on_rooms_list_update = callback
        if callback:
            callback(self.rooms)

    def set_room_state_callback(self, callback):
        with self._room_state_lock:
            self.on_room_state_update = callback
            pending_messages = self._pending_room_state_messages
            self._pending_room_state_messages = []

        if callback:
            for message in pending_messages:
                callback(message)

    def _dispatch_room_state(self, message):
        with self._room_state_lock:
            callback = self.on_room_state_update
            if not callback:
                self._pending_room_state_messages.append(message)
                return

        callback(message)

    def get_match_history(self):
        self.send_message({"action": "get_match_history"})

    def send_lobby_chat(self, message, match_id=None, attachment=None):
        payload = {"action": "lobby_chat", "message": message}
        if match_id:
            payload["match_id"] = match_id
        if attachment:
            payload["attachment"] = attachment
        self.send_message(payload)

    def request_cloudinary_signature(self, resource_type, callback):
        request_id = uuid.uuid4().hex
        with self._request_lock:
            self._request_callbacks[request_id] = callback
        try:
            self.send_message({
                "action": "cloudinary_signature",
                "resource_type": resource_type,
                "request_id": request_id
            })
        except Exception:
            with self._request_lock:
                self._request_callbacks.pop(request_id, None)
            raise

    def request_cloudinary_signature_sync(self, resource_type):
        if not self._auth_token:
            raise ConnectionError("Hãy đăng nhập lại để tải tệp lên.")

        request_id = uuid.uuid4().hex

        def receive_response(connection, buffered):
            while b"\n" not in buffered:
                data = connection.recv(2048)
                if not data:
                    raise ConnectionError("Máy chủ đóng kết nối khi cấp chữ ký.")
                buffered += data
                if len(buffered) > MAX_MESSAGE_BYTES:
                    raise ValueError("Phản hồi từ máy chủ vượt giới hạn.")
            line, remainder = buffered.split(b"\n", 1)
            response = json.loads(line.decode("utf-8"))
            if not isinstance(response, dict):
                raise ValueError("Máy chủ trả về dữ liệu không hợp lệ.")
            return response, remainder

        with socket.create_connection((self.host, self.port), timeout=10) as connection:
            connection.settimeout(10)
            connection.sendall(json.dumps({
                "action": "login",
                "token": self._auth_token
            }).encode("utf-8") + b"\n")
            login_response, buffered = receive_response(connection, b"")
            if login_response.get("status") != "success":
                raise PermissionError(
                    login_response.get("message", "Không thể xác thực tài khoản.")
                )

            connection.sendall(json.dumps({
                "action": "cloudinary_signature",
                "resource_type": resource_type,
                "request_id": request_id
            }).encode("utf-8") + b"\n")
            while True:
                response, buffered = receive_response(connection, buffered)
                if response.get("request_id") != request_id:
                    continue
                if response.get("status") == "error":
                    raise RuntimeError(
                        response.get("message", "Không thể cấp chữ ký upload.")
                    )
                if response.get("action") != "cloudinary_signature":
                    raise ValueError("Chữ ký upload từ máy chủ không hợp lệ.")
                return response

    def get_match_chat(self, match_id):
        self.send_message({"action": "get_match_chat", "match_id": match_id})

    def get_shared_match_record(self, match_id):
        self.send_message({
            "action": "get_shared_match_record",
            "match_id": match_id
        })

    def get_lobby_chat_history(self):
        self.send_message({"action": "get_lobby_chat_history"})

    def _resolve_request_callback(self, request_id, response):
        if not isinstance(request_id, str):
            return False
        with self._request_lock:
            callback = self._request_callbacks.pop(request_id, None)
        if not callback:
            return False
        callback(response)
        return True

    def send_message(self, message):
        payload = json.dumps(message).encode('utf-8') + b'\n'
        with self._send_lock:
            if not self.is_connected or not self.client_socket:
                raise ConnectionError("Chưa kết nối tới máy chủ.")
            self.client_socket.sendall(payload)

    def _receive_message(self):
        while True:
            while b'\n' not in self._receive_buffer:
                if not self.client_socket:
                    return None
                data = self.client_socket.recv(2048)
                if not data:
                    return None
                self._receive_buffer += data
                if len(self._receive_buffer) > MAX_MESSAGE_BYTES:
                    raise ValueError("Gói tin từ máy chủ vượt quá giới hạn cho phép.")

            message_data, self._receive_buffer = self._receive_buffer.split(b'\n', 1)
            if not message_data:
                continue
            message = json.loads(message_data.decode('utf-8'))
            if not isinstance(message, dict):
                raise ValueError("Máy chủ gửi dữ liệu không đúng định dạng.")
            return message

    def connect_and_login(self, id_token):
        """Mở Socket tới Master Server và ném Token lên để xác thực"""
        self.last_error = None
        self._auth_token = id_token
        try:
            # 1. Mở kết nối TCP
            self.client_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.client_socket.settimeout(5) # Đợi tối đa 5 giây
            self.client_socket.connect((self.host, self.port))
            self.client_socket.settimeout(None) # Hủy timeout sau khi kết nối xong
            self.is_connected = True
            
            print(f"[*] Đã mở kết nối TCP tới {self.host}:{self.port}")

            # 2. Đóng gói idToken vào JSON và gửi đi
            login_payload = {
                "action": "login",
                "token": id_token
            }
            self._receive_buffer = b''
            self.send_message(login_payload)

            # 3. Chờ Server Master kiểm duyệt qua Firebase và phản hồi
            response = self._receive_message()
            if response is None:
                raise ConnectionError("Máy chủ đã đóng kết nối trong khi đăng nhập.")

            if response.get("status") == "success":
                self.user_data = response.get("user_data", {})
                self.online_users = response.get("users", [])
                self.rooms = response.get("rooms", [])
                self.is_connected = True
                print("[+] Socket đã được xác thực hợp lệ. Bạn đang ở trong Sảnh!")
                
                # Bật một luồng (thread) chạy ngầm để liên tục lắng nghe dữ liệu từ Server
                self._start_listener()
                return True
            else:
                error_msg = response.get("message", "Lỗi không xác định")
                self.last_error = error_msg
                print(f"[!] Server từ chối kết nối: {error_msg}")
                self.disconnect()
                return False

        except Exception as e:
            self.last_error = str(e)
            print(f"[!] Lỗi kết nối Socket: {e}")
            self.disconnect()
            return False

    def listen_for_server_messages(self):
        while self.is_connected:
            try:
                message = self._receive_message()
                if message is None:
                    break
                
                # --- NẾU LÀ GÓI TIN CẬP NHẬT DANH SÁCH ---
                if message.get("action") == "update_online_list":
                    online_users = message.get("users", [])
                    self.online_users = online_users
                    # Kích hoạt hàm bên UI (nếu đã được gán)
                    if self.on_online_list_update:
                        self.on_online_list_update(online_users)
                elif message.get("action") == "update_rooms_list":
                    rooms = message.get("rooms", [])
                    self.rooms = rooms
                    if self.on_rooms_list_update:
                        self.on_rooms_list_update(rooms)
                elif message.get("action") == "match_history":
                    records = message.get("records", [])
                    if self.on_match_history_update:
                        self.on_match_history_update(records)
                elif message.get("action") == "lobby_chat_history":
                    if self.on_lobby_chat_history:
                        self.on_lobby_chat_history(message.get("messages", []))
                elif message.get("action") == "match_chat_history":
                    if self.on_match_chat_history:
                        self.on_match_chat_history(
                            message.get("match_id"),
                            message.get("messages", [])
                        )
                elif message.get("action") == "shared_match_record":
                    if self.on_shared_match_record:
                        self.on_shared_match_record(message.get("record", {}))
                elif message.get("action") == "cloudinary_signature":
                    self._resolve_request_callback(
                        message.get("request_id"),
                        message
                    )
                elif message.get("action") in (
                    "match_record_saved",
                    "match_record_save_failed"
                ):
                    if self.on_match_record_save_status:
                        self.on_match_record_save_status(message)
                elif message.get("action") in (
                    "call_invite",
                    "call_invite_sent",
                    "call_rejected",
                    "call_invite_timeout",
                    "call_started",
                    "call_ended",
                    "call_invite_cancelled"
                ):
                    if self.on_call_event:
                        self.on_call_event(message)
                elif message.get("type") == "lobby_chat":
                    if self.on_lobby_chat_received:
                        self.on_lobby_chat_received(message)
                elif message.get("action") == "handoff":
                    node_ip = message.get("node_ip")
                    node_port = message.get("node_port")
                    match_token = message.get("token")
                    mode = message.get("mode", "1vs1")
                    seat = message.get("seat", 0)
                    username = message.get("username", "")
                    uid = message.get("uid", "")
                    
                    print(f"[*] Nhận lệnh Handoff. Bắt đầu chuyển tới {node_ip}:{node_port}")
                    self.execute_handoff(
                        node_ip, node_port, match_token, mode, seat, username, uid
                    )
                    return
                elif message.get("status") == "error":
                    if self._resolve_request_callback(
                        message.get("request_id"),
                        message
                    ):
                        continue
                    if self.on_server_error:
                        self.on_server_error(message.get("message", "Yêu cầu thất bại."))
                elif message.get("type") == "chat":
                    sender = message.get("sender")
                    text = message.get("message")
                    print(f"[Chat] {sender}: {text}")
                    
                    if self.on_chat_received:
                        self.on_chat_received(
                            sender,
                            text,
                            message.get("attachment")
                        )
                elif message.get("type") == "audio":
                    sender = message.get("sender", "Người chơi")
                    audio_data = message.get("audio")
                    if audio_data and self.on_audio_received:
                        self.on_audio_received(sender, audio_data)
                elif message.get("type") == "audio_error":
                    if self.on_server_error:
                        self.on_server_error(message.get("message", "Không thể gửi tin nhắn thoại."))
                elif message.get("type") == "move_result":
                    if message.get("status") == "valid":
                        sender = message.get("sender")
                        move_uci = message.get("move")
                        is_check = message.get("is_check")
                        is_checkmate = message.get("is_checkmate")
                        is_stalemate = message.get("is_stalemate")
                        
                        # Đẩy dữ liệu sang UI
                        if self.on_move_received:
                            self.on_move_received(
                                sender,
                                move_uci,
                                is_check,
                                is_checkmate,
                                is_stalemate,
                                message.get("fen"),
                                message.get("game_over", False),
                                message.get("game_result")
                            )
                            
                    elif message.get("status") == "invalid":
                        error_message = message.get("message", "Nước đi không hợp lệ.")
                        print(f"[!] Lỗi đánh cờ: {error_message}")
                        if self.on_server_error:
                            self.on_server_error(error_message)
                elif message.get("action") in ("room_state", "game_start"):
                    self._dispatch_room_state(message)
                elif message.get("action") == "game_over":
                    if self.on_game_over:
                        self.on_game_over(message)
                elif message.get("status") in ("invalid", "error"):
                    if self.on_server_error:
                        self.on_server_error(message.get("message", "Yêu cầu không hợp lệ."))
            except Exception as e:
                print(f"[*] Mất kết nối tới máy chủ: {e}")
                break
        self.disconnect()

    def _start_listener(self):
        listener = threading.Thread(target=self.listen_for_server_messages, daemon=True)
        self._listener_thread = listener
        listener.start()

    def execute_handoff(self, node_ip, node_port, token, mode, seat, username, uid):
        with self._send_lock:
            master_socket = self.client_socket
            self.is_connected = False
            self.client_socket = None
            self._receive_buffer = b''
            if master_socket:
                master_socket.close()

        try:
            # Mở Socket mới tới Game Node (Cổng 8001)
            self.client_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.client_socket.settimeout(5)
            self.client_socket.connect((node_ip, node_port))
            self.client_socket.settimeout(None)
            self.is_connected = True
            
            # Gửi Token nộp cho Game Node
            join_payload = {
                "action": "join_game",
                "token": token,
                "mode": mode,
                "seat": seat,
                "username": username,
                "uid": uid
            }
            self.send_message(join_payload)
            
            # Nghe Game Node phản hồi xem Token có hợp lệ không
            response = self._receive_message()
            if response is None:
                raise ConnectionError("Game Node đã đóng kết nối khi tham gia phòng.")
            
            if response.get("status") == "success":
                print(f"[+] Handoff thành công! Đã yên vị trong phòng: {token}")
                response["node_ip"] = node_ip
                
                # Gọi callback để main.py đóng Sảnh chờ, mở Giao diện Bàn cờ
                if self.on_join_game_success:
                    self.on_join_game_success(token, response)
                
                # Bật lại luồng lắng nghe ĐỂ ĐÓN NƯỚC CỜ VÀ CHAT TỪ GAME NODE
                self._start_listener()
            else:
                error = response.get('message', 'Game Node từ chối tham gia phòng.')
                print(f"[!] Game Node từ chối Token: {error}")
                self.disconnect()
                if self.on_server_error:
                    self.on_server_error(error)
                
        except Exception as e:
            print(f"[!] Lỗi kết nối Game Node: {e}")
            self.disconnect()
            if self.on_server_error:
                self.on_server_error(str(e))

    def disconnect(self):
        """Đóng Socket và dọn dẹp"""
        self.is_connected = False
        with self._send_lock:
            client_socket = self.client_socket
            self.client_socket = None
            if client_socket:
                try:
                    client_socket.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                client_socket.close()
        self._receive_buffer = b''
        with self._request_lock:
            self._request_callbacks.clear()
        listener = self._listener_thread
        if listener and listener is not threading.current_thread():
            listener.join(timeout=2)
            if not listener.is_alive():
                self._listener_thread = None
        print("[-] Đã đóng kết nối TCP Client.")