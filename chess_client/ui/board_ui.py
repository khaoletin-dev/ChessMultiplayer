import base64
import io
import threading
import time
import webbrowser
import wave

import customtkinter as ctk
from tkinter import TclError, filedialog, messagebox
from network.udp_voice import UDPVoiceClient
from network.udp_call import UDPCallClient
from network.cloudinary_upload import (
    CLOUDINARY_CLOUD_NAME,
    describe_upload,
    upload_file
)

class BoardWindow(ctk.CTk):
    def __init__(self, room_token, tcp_client, username, player_info=None):
        super().__init__()
        
        self.title(f"Chess Online - Đang trong phòng: {room_token}")
        screen_width = self.winfo_screenwidth()
        screen_height = self.winfo_screenheight()
        board_size = max(
            480,
            min(760, screen_width - 500, screen_height - 180)
        )
        self.board_size = board_size
        self.geometry(
            f"{min(screen_width - 40, board_size + 500)}"
            f"x{min(screen_height - 60, board_size + 120)}"
        )
        self.minsize(
            min(1120, screen_width - 40),
            min(740, screen_height - 60)
        )
        self.configure(fg_color="#0B1220")
        
        self.room_token = room_token
        self.tcp_client = tcp_client
        self.username = username
        self.player_info = player_info or {}
        self.mode = self.player_info.get("mode", "1vs1")
        self.is_leader = self.player_info.get("is_leader", True)
        self.team = self.player_info.get("team", "white")
        self._game_started = (
            self.player_info.get("players", 0)
            >= self.player_info.get("capacity", 2 if self.mode == "1vs1" else 4)
        )
        self._game_over = False
        self._turn = self.player_info.get("turn", "white")
        self._voice_client = None
        self._voice_connecting = False
        self._closing = False
        self._returning_to_lobby = False
        self._file_upload_in_progress = False
        self._roster = self.player_info.get("roster", [])
        self._call_client = None
        self._call_window = None
        self._call_peer_name = None
        self._video_images = {}
        self._microphone_enabled = False
        self._camera_enabled = False
        self._pending_invite_id = None

        self.selected_square = None 
        self.last_move = None
        self._audio_stream = None
        self._audio_frames = []
        self._audio_sample_count = 0
        self._audio_lock = threading.Lock()
        self._record_started_at = 0.0
        self._max_record_seconds = 10
        self._audio_sample_rate = 16000
        self._max_audio_bytes = self._audio_sample_rate * self._max_record_seconds * 2 + 44
        self._chat_has_messages = False
        
        # Bắt sự kiện tắt cửa sổ
        self.protocol("WM_DELETE_WINDOW", self.on_closing)

        # ==========================================
        # 1. KHUNG BÀN CỜ (CỘT TRÁI)
        # ==========================================
        self.board_container = ctk.CTkFrame(self, fg_color="transparent")
        self.board_container.pack(side="left", padx=30, pady=30, fill="both", expand=True)
        self.board_container.grid_rowconfigure(0, weight=1)
        self.board_container.grid_columnconfigure(1, weight=1)
        self.board_frame = ctk.CTkFrame(
            self.board_container,
            width=self.board_size,
            height=self.board_size,
            fg_color="#111C2E",
            corner_radius=16,
            border_width=1,
            border_color="#23334D"
        )
        self.board_frame.grid(row=0, column=1, sticky="nsew", padx=(4, 0), pady=4)
        self.board_frame.grid_propagate(False)
        self.rank_frame = ctk.CTkFrame(self.board_container, fg_color="transparent")
        self.rank_frame.grid(row=0, column=0, sticky="nsew")
        for row in range(8):
            self.rank_frame.grid_rowconfigure(row, weight=1, uniform="rank_rows")
        
        self.buttons = {} # Lưu trữ 64 nút bấm (ô cờ)
        self.draw_board()
        self.setup_starting_position()

        # ==========================================
        # 2. KHUNG CHAT & THÔNG TIN (CỘT PHẢI)
        # ==========================================
        self.panel_frame = ctk.CTkFrame(
            self,
            width=360,
            corner_radius=16,
            fg_color="#111C2E",
            border_width=1,
            border_color="#23334D"
        )
        self.panel_frame.pack(side="right", padx=(0, 30), pady=30, fill="y")
        self.panel_frame.pack_propagate(False)
        
        self.lbl_info = ctk.CTkLabel(
            self.panel_frame,
            text=f"PHÒNG {room_token}",
            font=("Roboto", 20, "bold")
        )
        self.lbl_info.pack(pady=(18, 4))

        self.status_label = ctk.CTkLabel(
            self.panel_frame,
            text=f"Bạn đang chơi với tên {username}",
            text_color=("gray35", "gray75"),
            font=("Roboto", 13)
        )
        self.status_label.pack(pady=(0, 10))
        self.turn_label = ctk.CTkLabel(
            self.panel_frame,
            text=self._turn_status_text(),
            font=("Roboto", 13, "bold"),
            text_color="#73A7FF"
        )
        self.turn_label.pack(pady=(0, 10))

        if self.mode == "2vs2":
            team_name = "Trắng" if self.player_info.get("team") == "white" else "Đen"
            role_name = "Trưởng nhóm" if self.is_leader else "Thành viên"
            if not self._game_started:
                self.status_label.configure(
                    text=(
                        f"Đang chờ người chơi: "
                        f"{self.player_info.get('players', 1)}/"
                        f"{self.player_info.get('capacity', 4)}"
                    )
                )
            self.team_label = ctk.CTkLabel(
                self.panel_frame,
                text=f"Đội {team_name} · {role_name}",
                text_color=("#247A3B", "#8FD19E"),
                font=("Roboto", 13, "bold")
            )
            self.team_label.pack(pady=(0, 8))

        self.legend_label = ctk.CTkLabel(
            self.panel_frame,
            text=(
                "Chỉ trưởng nhóm chốt nước đi"
                if self.mode == "2vs2" and not self.is_leader
                else "Chọn quân rồi chọn ô muốn đi"
            ),
            text_color=("gray35", "gray75"),
            font=("Roboto", 13)
        )
        self.legend_label.pack(pady=(0, 8))

        self.game_actions = ctk.CTkFrame(self.panel_frame, fg_color="transparent")
        self.game_actions.pack(fill="x", padx=15, pady=(0, 6))
        self.draw_btn = ctk.CTkButton(
            self.game_actions,
            text="Tuyên bố hòa",
            height=36,
            fg_color="#263650",
            hover_color="#334967",
            command=self.claim_draw
        )
        self.draw_btn.pack(side="left", fill="x", expand=True, padx=(0, 5))
        self.resign_btn = ctk.CTkButton(
            self.game_actions,
            text="Đầu hàng",
            height=36,
            fg_color="#7B3442",
            hover_color="#963D4F",
            command=self.resign
        )
        self.resign_btn.pack(side="left", fill="x", expand=True, padx=(5, 0))
        
        # Khung hiển thị tin nhắn (Chỉ đọc)
        self.chat_display = ctk.CTkScrollableFrame(
            self.panel_frame,
            corner_radius=10,
            fg_color="#0E1929"
        )
        self.chat_display.pack(fill="both", expand=True, padx=15, pady=10)
        self.chat_empty_label = ctk.CTkLabel(
            self.chat_display,
            text="Tin nhắn trong ván đấu sẽ xuất hiện ở đây.",
            text_color=("gray45", "gray65")
        )
        self.chat_empty_label.pack(pady=12)
        
        # Khung nhập tin nhắn
        self.chat_input = ctk.CTkEntry(
            self.panel_frame,
            height=42,
            corner_radius=10,
            border_color="#344966",
            placeholder_text="Nhập tin nhắn..."
        )
        self.chat_input.pack(fill="x", padx=15, pady=(0, 10))
        self.chat_input.bind("<Return>", self._send_chat_on_enter)
        
        self.chat_actions = ctk.CTkFrame(self.panel_frame, fg_color="transparent")
        self.chat_actions.pack(fill="x", padx=15, pady=(0, 15))
        self.emoji_btn = ctk.CTkButton(
            self.chat_actions, text="😊", width=40, command=self.open_emoji_picker
        )
        self.emoji_btn.pack(side="left", padx=(0, 5))
        self.record_btn = ctk.CTkButton(
            self.chat_actions, text="🎙 Ghi âm", width=88, command=self.start_audio_recording
        )
        self.record_btn.pack(side="left", padx=5)
        self.stop_record_btn = ctk.CTkButton(
            self.chat_actions,
            text="■ Gửi audio",
            width=88,
            fg_color="#C62828",
            hover_color="#B71C1C",
            command=self.stop_and_send_recording,
            state="disabled"
        )
        self.stop_record_btn.pack(side="left", padx=5)
        self.send_btn = ctk.CTkButton(
            self.chat_actions, text="Gửi", width=48, command=self.send_chat
        )
        self.send_btn.pack(side="right", padx=(5, 0))
        self.attach_file_btn = ctk.CTkButton(
            self.chat_actions,
            text="📎",
            width=38,
            command=self.choose_chat_file
        )
        self.attach_file_btn.pack(side="left", padx=(0, 5))
        self.chat_status_label = ctk.CTkLabel(
            self.panel_frame,
            text="",
            anchor="w",
            text_color="#91A2BA",
            font=("Roboto", 11)
        )
        self.chat_status_label.pack(fill="x", padx=16, pady=(0, 6))
        self.return_lobby_btn = ctk.CTkButton(
            self.panel_frame,
            text="Về sảnh chính",
            height=38,
            fg_color="#28694A",
            hover_color="#32855D",
            command=self.return_to_lobby
        )
        self.return_lobby_btn.pack_forget()
        self.call_btn = ctk.CTkButton(
            self.panel_frame,
            text="📹 Gọi thoại / video",
            height=38,
            command=self.open_call_targets,
            state="normal" if self._game_started else "disabled"
        )
        self.call_btn.pack(fill="x", padx=15, pady=(0, 10))
        if self.mode == "2vs2":
            self.voice_btn = ctk.CTkButton(
                self.panel_frame,
                text="🎙 Bật voice đồng đội",
                command=self.toggle_team_voice,
                state="normal" if self._game_started else "disabled"
            )
            self.voice_btn.pack(fill="x", padx=15, pady=(0, 12))
        if self.tcp_client:
            self.tcp_client.on_chat_received = self.display_chat
            self.tcp_client.on_audio_received = self.display_audio
            self.tcp_client.on_move_received = self.display_move
            self.tcp_client.set_room_state_callback(self.display_room_state)
            self.tcp_client.on_game_over = self.display_game_over
            self.tcp_client.on_match_record_save_status = (
                self.display_match_record_save_status
            )
            self.tcp_client.on_call_event = self.display_call_event
            self.tcp_client.on_server_error = self._handle_game_server_error
        if self.player_info.get("fen"):
            self._set_board_from_fen(self.player_info["fen"])

    def _turn_status_text(self):
        return "Lượt Trắng" if self._turn == "white" else "Lượt Đen"

    def display_room_state(self, message):
        try:
            self.after(0, self._update_room_state, message)
        except TclError:
            pass

    def _update_room_state(self, message):
        self._game_started = (
            message.get("action") == "game_start"
            or message.get("status") == "playing"
        )
        self._game_over = message.get("status") == "game_over"
        players = message.get("players", 0)
        capacity = message.get("capacity", 4)
        roster = message.get("roster")
        if isinstance(roster, list):
            self._roster = roster
        if message.get("fen"):
            self._set_board_from_fen(message["fen"])
        if self._game_started:
            self.status_label.configure(text="Trận đấu đang diễn ra")
            self.legend_label.configure(
                text=(
                    "Chọn quân rồi chọn ô muốn đi"
                    if self.mode != "2vs2" or self.is_leader
                    else "Trao đổi với đồng đội · Trưởng nhóm chốt nước đi"
                )
            )
        else:
            self.status_label.configure(
                text=f"Đang chờ người chơi: {players}/{capacity}"
            )
            self.legend_label.configure(text="Chờ đủ người chơi để bắt đầu")
        if self.mode == "2vs2":
            if self._game_started:
                self.voice_btn.configure(state="normal")
            else:
                self.voice_btn.configure(state="disabled")
                if self._voice_client:
                    self._voice_client.stop()
                    self._voice_client = None
                    self.voice_btn.configure(text="🎙 Bật voice đồng đội")
        self.call_btn.configure(
            state="normal" if self._game_started and not self._game_over
            else "disabled"
        )
        self._update_game_controls()

    def toggle_team_voice(self):
        if self._voice_connecting:
            return
        if self._voice_client:
            self._voice_client.stop()
            self._voice_client = None
            self.voice_btn.configure(text="🎙 Bật voice đồng đội")
            return

        voice_token = self.player_info.get("voice_token")
        voice_host = self.player_info.get("node_ip")
        voice_port = self.player_info.get("voice_port")
        if not voice_token or not voice_host or not voice_port:
            self.show_chat_error("Thông tin kết nối voice UDP không đầy đủ.")
            return

        voice_client = UDPVoiceClient(
            voice_host,
            voice_port,
            voice_token,
            on_error=self._show_voice_error
        )
        self._voice_connecting = True
        self.voice_btn.configure(state="disabled", text="Đang kết nối voice...")
        threading.Thread(
            target=self._start_voice_worker,
            args=(voice_client,),
            daemon=True
        ).start()

    def open_call_targets(self):
        if not self._game_started or self._game_over:
            return
        if self._call_client or self._pending_invite_id:
            return
        targets = [
            player for player in self._roster
            if player.get("seat") != self.player_info.get("seat")
        ]
        if not targets:
            self.show_chat_error("Chưa có người chơi khác trong phòng để gọi.")
            return
        if len(targets) == 1:
            self._invite_call_target(targets[0])
            return

        picker = ctk.CTkToplevel(self)
        picker.title("Chọn người để gọi")
        picker.geometry("320x280")
        picker.resizable(False, False)
        picker.transient(self)
        ctk.CTkLabel(
            picker,
            text="Gọi thoại / video",
            font=("Roboto", 17, "bold")
        ).pack(padx=16, pady=(16, 8))
        ctk.CTkLabel(
            picker,
            text="Camera và micro sẽ tắt cho đến khi bạn bật.",
            text_color="#91A2BA",
            wraplength=270
        ).pack(padx=16, pady=(0, 10))
        for player in targets:
            team_name = "Trắng" if player.get("team") == "white" else "Đen"
            ctk.CTkButton(
                picker,
                text=f"{player.get('username', 'Người chơi')} · {team_name}",
                command=lambda selected=player: (
                    picker.destroy(),
                    self._invite_call_target(selected)
                )
            ).pack(fill="x", padx=18, pady=5)

    def _invite_call_target(self, target):
        if not self.tcp_client or not self.tcp_client.is_connected:
            self.show_chat_error("Mất kết nối tới phòng chơi.")
            return
        try:
            self.tcp_client.send_message({
                "type": "call_invite",
                "target_seat": target.get("seat")
            })
            self.chat_status_label.configure(
                text=f"Đang mời {target.get('username', 'người chơi')}..."
            )
        except (ConnectionError, OSError) as exc:
            self.show_chat_error(str(exc))

    def display_call_event(self, message):
        try:
            self.after(0, self._handle_call_event, message)
        except TclError:
            pass

    def _handle_call_event(self, message):
        action = message.get("action")
        if action == "call_invite":
            caller = message.get("caller_name", "Người chơi")
            call_id = message.get("call_id")
            if (
                self._closing
                or not self._game_started
                or self._game_over
                or self._call_client
                or self._pending_invite_id
            ):
                if call_id and self.tcp_client and self.tcp_client.is_connected:
                    self.tcp_client.send_message({
                        "type": "call_response",
                        "call_id": call_id,
                        "accepted": False
                    })
                return
            accepted = messagebox.askyesno(
                "Lời mời gọi",
                f"{caller} muốn gọi thoại/video với bạn.\n"
                "Camera và micro của bạn đang tắt. Chấp nhận?",
                parent=self
            )
            try:
                self.tcp_client.send_message({
                    "type": "call_response",
                    "call_id": call_id,
                    "accepted": accepted
                })
                if accepted:
                    self._pending_invite_id = call_id
            except (ConnectionError, OSError) as exc:
                self.show_chat_error(str(exc))
        elif action == "call_invite_sent":
            self._pending_invite_id = message.get("call_id")
            self.chat_status_label.configure(
                text=f"Đang chờ {message.get('target_name', 'người chơi')} chấp nhận..."
            )
        elif action == "call_rejected":
            self._pending_invite_id = None
            self.chat_status_label.configure(
                text=f"{message.get('target_name', 'Người chơi')} đã từ chối cuộc gọi."
            )
        elif action == "call_invite_timeout":
            self._pending_invite_id = None
            self.chat_status_label.configure(text="Lời mời gọi đã hết hạn.")
        elif action == "call_invite_cancelled":
            self._pending_invite_id = None
            self.chat_status_label.configure(text="Lời mời gọi đã bị hủy.")
        elif action == "call_started":
            self._pending_invite_id = None
            peer = message.get("peer", {})
            self._start_call(peer)
        elif action == "call_ended":
            self._finish_call_window(
                message.get("message", "Cuộc gọi đã kết thúc."),
                notify_server=False
            )

    def _handle_game_server_error(self, message):
        if self._pending_invite_id == "requesting":
            self._pending_invite_id = None
        self.show_chat_error(message)

    def _start_call(self, peer):
        voice_token = self.player_info.get("voice_token")
        voice_host = self.player_info.get("node_ip")
        voice_port = self.player_info.get("voice_port")
        if not voice_token or not voice_host or not voice_port:
            self.show_chat_error("Thiếu thông tin kết nối cuộc gọi UDP.")
            self.tcp_client.send_message({"type": "call_end"})
            return
        if self._call_client:
            return
        if self._voice_client:
            self._voice_client.stop()
            self._voice_client = None
            if self.mode == "2vs2":
                self.voice_btn.configure(text="🎙 Bật voice đồng đội")
        self._call_peer_name = peer.get("username", "Người chơi")
        self._show_call_window()
        call_client = UDPCallClient(
            voice_host,
            voice_port,
            voice_token,
            on_remote_video=lambda frame: self._schedule_video_frame(False, frame),
            on_local_video=lambda frame: self._schedule_video_frame(True, frame),
            on_error=self._show_call_error
        )
        self._call_client = call_client
        self.call_status_label.configure(text="Đang kết nối media UDP...")
        threading.Thread(
            target=self._start_call_worker,
            args=(call_client,),
            daemon=True
        ).start()

    def _start_call_worker(self, call_client):
        try:
            call_client.start()
        except Exception as exc:
            try:
                self.after(0, self._finish_call_start, call_client, str(exc))
            except TclError:
                call_client.stop()
            return
        try:
            self.after(0, self._finish_call_start, call_client, None)
        except TclError:
            call_client.stop()

    def _finish_call_start(self, call_client, error):
        if error or self._closing or self._call_client is not call_client:
            call_client.stop()
            if self._call_client is call_client:
                self._call_client = None
            if error and not self._closing:
                self._finish_call_window(
                    "Không thể kết nối cuộc gọi.",
                    notify_server=True
                )
                self.show_chat_error(f"Không thể bắt đầu cuộc gọi UDP: {error}")
            return
        self.call_status_label.configure(
            text=f"Đang gọi {self._call_peer_name} · mic và camera đang tắt"
        )

    def _show_call_window(self):
        if self._call_window and self._call_window.winfo_exists():
            self._call_window.focus()
            return
        window = ctk.CTkToplevel(self)
        window.title(f"Cuộc gọi với {self._call_peer_name}")
        window.geometry("660x390")
        window.minsize(600, 360)
        window.transient(self)
        for column in range(2):
            window.grid_columnconfigure(column, weight=1)
        window.grid_rowconfigure(1, weight=1)
        ctk.CTkLabel(
            window,
            text=f"Cuộc gọi 1–1 · {self._call_peer_name}",
            font=("Roboto", 17, "bold")
        ).grid(row=0, column=0, columnspan=2, pady=(14, 8))

        self.local_video_label = self._make_video_tile(
            window,
            "Bạn · camera đang tắt",
            row=1,
            column=0
        )
        self.remote_video_label = self._make_video_tile(
            window,
            f"{self._call_peer_name} · camera đang tắt",
            row=1,
            column=1
        )
        controls = ctk.CTkFrame(window, fg_color="transparent")
        controls.grid(row=2, column=0, columnspan=2, pady=(10, 14))
        self.microphone_button = ctk.CTkButton(
            controls,
            text="Bật mic",
            width=120,
            command=self.toggle_call_microphone
        )
        self.microphone_button.pack(side="left", padx=6)
        self.camera_button = ctk.CTkButton(
            controls,
            text="Bật camera",
            width=120,
            command=self.toggle_call_camera
        )
        self.camera_button.pack(side="left", padx=6)
        self.call_status_label = ctk.CTkLabel(
            window,
            text="Camera và mic đang tắt",
            text_color="#91A2BA"
        )
        self.call_status_label.grid(row=3, column=0, columnspan=2, pady=(0, 6))
        self.end_call_button = ctk.CTkButton(
            window,
            text="Kết thúc cuộc gọi",
            width=150,
            fg_color="#8E3345",
            hover_color="#A83F53",
            command=lambda: self._finish_call_window(
                "Bạn đã kết thúc cuộc gọi.",
                notify_server=True
            )
        )
        self.end_call_button.grid(row=4, column=0, columnspan=2, pady=(0, 12))
        window.protocol(
            "WM_DELETE_WINDOW",
            lambda: self._finish_call_window(
                "Bạn đã kết thúc cuộc gọi.",
                notify_server=True
            )
        )
        self._call_window = window

    def _make_video_tile(self, parent, placeholder, row, column):
        tile = ctk.CTkFrame(parent, fg_color="#101B2C", corner_radius=10)
        tile.grid(row=row, column=column, sticky="nsew", padx=10, pady=8)
        tile.grid_rowconfigure(0, weight=1)
        tile.grid_columnconfigure(0, weight=1)
        label = ctk.CTkLabel(
            tile,
            text=placeholder,
            text_color="#C8D4E5",
            wraplength=260
        )
        label.grid(row=0, column=0, sticky="nsew", padx=8, pady=8)
        return label

    def toggle_call_microphone(self):
        call_client = self._call_client
        if not call_client:
            return
        enabled = not self._microphone_enabled
        try:
            call_client.set_microphone_enabled(enabled)
        except Exception as exc:
            self.show_chat_error(f"Không thể đổi trạng thái micro: {exc}")
            return
        self._microphone_enabled = enabled
        self.microphone_button.configure(
            text="Tắt mic" if enabled else "Bật mic",
            fg_color="#8E3345" if enabled else "#263650"
        )
        self._update_call_status()

    def toggle_call_camera(self):
        call_client = self._call_client
        if not call_client:
            return
        enabled = not self._camera_enabled
        call_client.set_camera_enabled(enabled)
        self._camera_enabled = enabled
        self.camera_button.configure(
            text="Tắt camera" if enabled else "Bật camera",
            fg_color="#8E3345" if enabled else "#263650"
        )
        if not enabled:
            self.local_video_label.configure(image=None, text="Bạn · camera đang tắt")
            self.local_video_label.image = None
            self._video_images.pop("local", None)
        self._update_call_status()

    def _update_call_status(self):
        microphone = "mic bật" if self._microphone_enabled else "mic tắt"
        camera = "camera bật" if self._camera_enabled else "camera tắt"
        self.call_status_label.configure(text=f"{microphone} · {camera}")

    def _schedule_video_frame(self, local, frame_data):
        try:
            self.after(0, self._display_video_frame, local, frame_data)
        except TclError:
            pass

    def _display_video_frame(self, local, frame_data):
        if not self._call_window or not self._call_window.winfo_exists():
            return
        try:
            from PIL import Image

            image = Image.open(io.BytesIO(frame_data)).convert("RGB")
            image.thumbnail((280, 210))
            ctk_image = ctk.CTkImage(
                light_image=image,
                dark_image=image,
                size=image.size
            )
            label = self.local_video_label if local else self.remote_video_label
            label.configure(image=ctk_image, text="")
            label.image = ctk_image
            self._video_images["local" if local else "remote"] = ctk_image
        except (OSError, ValueError) as exc:
            self._show_call_error(f"Không thể hiển thị hình ảnh cuộc gọi: {exc}")

    def _show_call_error(self, message):
        try:
            self.after(0, self._handle_call_error, message)
        except TclError:
            print(f"[!] Cuộc gọi video: {message}")

    def _handle_call_error(self, message):
        if (
            "camera" in message.lower()
            or "opencv" in message.lower()
        ):
            self._camera_enabled = False
            if self._call_window and self._call_window.winfo_exists():
                self.camera_button.configure(
                    text="Bật camera",
                    fg_color="#263650"
                )
                self.local_video_label.configure(
                    image=None,
                    text="Bạn · camera đang tắt"
                )
                self.local_video_label.image = None
                self._video_images.pop("local", None)
                self._update_call_status()
        self.show_chat_error(message)

    def _finish_call_window(self, message, notify_server):
        if notify_server and self.tcp_client and self.tcp_client.is_connected:
            try:
                self.tcp_client.send_message({"type": "call_end"})
            except (ConnectionError, OSError) as exc:
                self.show_chat_error(str(exc))
        call_client = self._call_client
        self._call_client = None
        if call_client:
            call_client.stop()
        self._microphone_enabled = False
        self._camera_enabled = False
        self._video_images.clear()
        window = self._call_window
        self._call_window = None
        if window and window.winfo_exists():
            window.destroy()
        self.chat_status_label.configure(text=message)
        self._pending_invite_id = None

    def _start_voice_worker(self, voice_client):
        try:
            voice_client.start()
        except Exception as exc:
            voice_client.stop()
            try:
                self.after(0, self._finish_voice_start, voice_client, str(exc))
            except TclError:
                pass
            return
        try:
            self.after(0, self._finish_voice_start, voice_client, None)
        except TclError:
            voice_client.stop()

    def _finish_voice_start(self, voice_client, error):
        self._voice_connecting = False
        if self._closing or error:
            voice_client.stop()
            self.voice_btn.configure(
                state="normal" if self._game_started and not self._game_over else "disabled",
                text="🎙 Bật voice đồng đội"
            )
            if error and not self._closing:
                self.show_chat_error(f"Không thể bật voice UDP: {error}")
            return
        self._voice_client = voice_client
        self.voice_btn.configure(
            state="normal",
            text="🔇 Tắt voice đồng đội"
        )

    def _show_voice_error(self, message):
        try:
            self.after(0, self.show_chat_error, message)
        except TclError:
            print(f"[!] Voice: {message}")

    def display_move(
        self, sender, move_uci, is_check, is_checkmate, is_stalemate,
        fen=None, game_over=False, game_result=None
    ):
        """Hứng lệnh cờ từ luồng mạng, đẩy vào luồng UI chính"""
        self.after(
            0,
            self._process_ui_move,
            sender,
            move_uci,
            is_check,
            is_checkmate,
            is_stalemate,
            fen,
            game_over,
            game_result
        )
    def _to_uci(self, row, col):
        """Dịch tọa độ Hàng/Cột (0-7) sang chuẩn cờ vua (a-h, 1-8)"""
        col_char = chr(col + ord('a')) # 0 -> 'a', 4 -> 'e'
        row_char = str(8 - row)        # 6 -> '2'
        return f"{col_char}{row_char}"
    def _process_ui_move(
        self, sender, move_uci, is_check, is_checkmate, is_stalemate,
        fen, game_over, game_result
    ):
        start_col = ord(move_uci[0]) - ord('a')
        start_row = 8 - int(move_uci[1])
        end_col = ord(move_uci[2]) - ord('a')
        end_row = 8 - int(move_uci[3])
        if fen:
            self._set_board_from_fen(fen)
        self.last_move = ((start_row, start_col), (end_row, end_col))
        self.selected_square = None
        self._refresh_square_colors()
        if is_check and not is_checkmate:
            self._insert_chat("HỆ THỐNG", f"{sender} chiếu tướng.")
        if game_over:
            self._game_over = True
            self.turn_label.configure(text=f"Ván cờ kết thúc · {game_result}")
            self._update_game_controls()

    def display_game_over(self, message):
        self.after(0, self._show_game_over, message)

    def _show_game_over(self, message):
        self._game_over = True
        self.call_btn.configure(state="disabled")
        if self._call_client:
            self._finish_call_window(
                "Cuộc gọi đã kết thúc cùng ván đấu.",
                notify_server=True
            )
        self.status_label.configure(text="Ván cờ đã kết thúc")
        self.turn_label.configure(
            text=f"Kết quả {message.get('result', '')}".strip()
        )
        self._insert_chat(
            "HỆ THỐNG",
            message.get("message", "Ván cờ đã kết thúc.")
        )
        self.chat_status_label.configure(
            text="Đang lưu biên bản trận lên Firebase..."
        )
        self.return_lobby_btn.pack(fill="x", padx=15, pady=(0, 12))
        self.after(10000, self.return_to_lobby)
        self._update_game_controls()

    def display_match_record_save_status(self, message):
        self.after(0, self._show_match_record_save_status, message)

    def _show_match_record_save_status(self, message):
        if message.get("action") == "match_record_saved":
            self.chat_status_label.configure(
                text="Đã lưu biên bản trận và chat lên Firebase."
            )
            self.after(1500, self.return_to_lobby)
        else:
            self.chat_status_label.configure(
                text="Lưu biên bản thất bại; xem lỗi trong terminal máy chủ."
            )

    def return_to_lobby(self):
        if self._returning_to_lobby:
            return
        self._returning_to_lobby = True
        self.on_closing()

    def _set_board_from_fen(self, fen):
        fen_pieces = {
            "p": "♟", "r": "♜", "n": "♞", "b": "♝", "q": "♛", "k": "♚",
            "P": "♙", "R": "♖", "N": "♘", "B": "♗", "Q": "♕", "K": "♔"
        }
        fields = fen.split()
        if len(fields) < 2:
            return
        ranks = fields[0].split("/")
        if len(ranks) != 8:
            return
        board_pieces = []
        for row, rank in enumerate(ranks):
            col = 0
            row_pieces = []
            for value in rank:
                if value.isdigit():
                    count = int(value)
                    if count < 1 or col + count > 8:
                        return
                    row_pieces.extend([""] * count)
                    col += count
                elif value in fen_pieces and col < 8:
                    row_pieces.append(fen_pieces[value])
                    col += 1
                else:
                    return
            if col != 8:
                return
            board_pieces.append(row_pieces)

        for row, row_pieces in enumerate(board_pieces):
            for col in range(8):
                piece = row_pieces[col]
                self.buttons[(row, col)].configure(
                    text=piece,
                    text_color=self._piece_color(piece, row, col)
                )
        self._refresh_square_colors()
        self._turn = "white" if fields[1] == "w" else "black"
        self.turn_label.configure(text=self._turn_status_text())
        self._update_game_controls()

    def _update_game_controls(self):
        is_my_turn = self._turn == self.team
        can_play = self._game_started and not self._game_over and is_my_turn
        can_claim = self._game_started and not self._game_over
        self.draw_btn.configure(state="normal" if can_claim else "disabled")
        self.resign_btn.configure(state="normal" if can_claim else "disabled")
        if self.mode == "2vs2" and not self.is_leader:
            self.turn_label.configure(
                text=f"{self._turn_status_text()} · Trưởng nhóm chốt nước đi"
            )
        elif can_play:
            self.turn_label.configure(
                text=f"{self._turn_status_text()} · Lượt của bạn"
            )
        elif self._game_started and not self._game_over:
            self.turn_label.configure(
                text=f"{self._turn_status_text()} · Đang chờ đối thủ"
            )
        if not self._game_started:
            self.turn_label.configure(text="Chờ đủ người chơi")
    def draw_board(self):
        """Vẽ bàn cờ tương phản cao, có tọa độ hàng và cột."""
        self.board_container.grid_columnconfigure(0, weight=0, minsize=28)
        self.board_container.grid_rowconfigure(1, weight=0, minsize=28)
        for index, file_name in enumerate("abcdefgh"):
            label = ctk.CTkLabel(
                self.board_container,
                text=file_name,
                font=("Roboto", 14, "bold"),
                text_color="#C8D4E5"
            )
            label.grid(row=1, column=index + 1, sticky="nsew")

        for row in range(8):
            rank_label = ctk.CTkLabel(
                self.rank_frame,
                text=str(8 - row),
                font=("Roboto", 14, "bold"),
                text_color="#C8D4E5"
            )
            rank_label.grid(row=row, column=0, sticky="nsew")
            self.board_frame.grid_rowconfigure(row, weight=1, uniform="board_rows")
            self.board_frame.grid_columnconfigure(
                row,
                weight=1,
                uniform="board_columns"
            )
        
        for row in range(8):
            for col in range(8):
                color = self._base_square_color(row, col)
                btn = ctk.CTkButton(
                    self.board_frame, 
                    text="", 
                    font=("Segoe UI Symbol", 50, "bold"),
                    fg_color=color, 
                    hover_color=self._square_hover_color(row, col),
                    text_color="#25221F",
                    corner_radius=2,
                    border_width=1,
                    border_color="#23334D",
                    command=lambda r=row, c=col: self.square_clicked(r, c)
                )
                btn.grid(row=row, column=col, sticky="nsew", padx=1, pady=1)
                self.buttons[(row, col)] = btn

    def _base_square_color(self, row, col):
        return "#F0DDBB" if (row + col) % 2 == 0 else "#547A65"

    def _square_hover_color(self, row, col):
        return "#F9EACF" if (row + col) % 2 == 0 else "#689276"

    def _piece_color(self, piece, row, col):
        if not piece:
            return self._base_square_color(row, col)
        is_white_piece = piece in "♙♖♘♗♕♔"
        return "#FFFDF5" if is_white_piece else "#111820"

    def _refresh_square_colors(self):
        last_move_squares = set(self.last_move or ())
        for (row, col), button in self.buttons.items():
            color = self._base_square_color(row, col)
            border_color = "#23334D"
            border_width = 1
            if (row, col) in last_move_squares:
                color = "#D6ED87" if (row + col) % 2 == 0 else "#A8D76D"
                border_color = "#FFF2B3"
                border_width = 2
            if self.selected_square == (row, col):
                color = "#FFD166"
                border_color = "#FFF8E7"
                border_width = 3
            piece = button.cget("text")
            button.configure(
                fg_color=color,
                hover_color="#FFE08A" if self.selected_square == (row, col)
                else self._square_hover_color(row, col),
                border_color=border_color,
                border_width=border_width,
                text_color=self._piece_color(piece, row, col)
            )

    def setup_starting_position(self):
        """Bày trận hình cờ vua tiêu chuẩn bằng ký tự Unicode"""
        initial_board = [
            ["♜", "♞", "♝", "♛", "♚", "♝", "♞", "♜"], # Hàng 0: Sĩ quan Đen
            ["♟", "♟", "♟", "♟", "♟", "♟", "♟", "♟"], # Hàng 1: Tốt Đen
            ["", "", "", "", "", "", "", ""],           # Hàng 2
            ["", "", "", "", "", "", "", ""],           # Hàng 3
            ["", "", "", "", "", "", "", ""],           # Hàng 4
            ["", "", "", "", "", "", "", ""],           # Hàng 5
            ["♙", "♙", "♙", "♙", "♙", "♙", "♙", "♙"], # Hàng 6: Tốt Trắng
            ["♖", "♘", "♗", "♕", "♔", "♗", "♘", "♖"]  # Hàng 7: Sĩ quan Trắng
        ]
        
        for row in range(8):
            for col in range(8):
                piece = initial_board[row][col]
                self.buttons[(row, col)].configure(
                    text=piece,
                    text_color=self._piece_color(piece, row, col)
                )

    def square_clicked(self, row, col):
        if (
            not self._game_started
            or self._game_over
            or self._turn != self.team
            or (self.mode == "2vs2" and not self.is_leader)
        ):
            return

        if self.selected_square is None:
            piece_text = self.buttons[(row, col)].cget("text")
            if piece_text and ((piece_text in "♙♖♘♗♕♔") == (self.team == "white")):
                self.selected_square = (row, col)
                self._refresh_square_colors()
        else:
            start_row, start_col = self.selected_square
            if (start_row, start_col) == (row, col):
                self.selected_square = None
                self._refresh_square_colors()
                return

            target_piece = self.buttons[(row, col)].cget("text")
            selected_piece = self.buttons[(start_row, start_col)].cget("text")
            white_pieces = "♙♖♘♗♕♔"
            if target_piece and ((target_piece in white_pieces) == (selected_piece in white_pieces)):
                self.selected_square = (row, col)
                self._refresh_square_colors()
                return

            start_uci = self._to_uci(start_row, start_col)
            end_uci = self._to_uci(row, col)
            move_uci = f"{start_uci}{end_uci}"
            reaches_last_rank = (
                selected_piece == "♙" and row == 0
                or selected_piece == "♟" and row == 7
            )
            if reaches_last_rank:
                self._choose_promotion(move_uci)
            else:
                self._submit_move(move_uci)

    def _choose_promotion(self, move_uci):
        dialog = ctk.CTkToplevel(self)
        dialog.title("Phong cấp")
        dialog.geometry("300x140")
        dialog.resizable(False, False)
        dialog.transient(self)
        dialog.grab_set()
        ctk.CTkLabel(
            dialog,
            text="Chọn quân cờ muốn phong cấp",
            font=("Roboto", 14, "bold")
        ).pack(pady=(18, 12))

        options = ctk.CTkFrame(dialog, fg_color="transparent")
        options.pack(fill="x", padx=12)
        for label, promotion in (
            ("Hậu", "q"), ("Xe", "r"), ("Tượng", "b"), ("Mã", "n")
        ):
            ctk.CTkButton(
                options,
                text=label,
                width=62,
                command=lambda value=promotion: self._finish_promotion(
                    dialog, f"{move_uci}{value}"
                )
            ).pack(side="left", expand=True, padx=3)

    def _finish_promotion(self, dialog, move_uci):
        dialog.destroy()
        self._submit_move(move_uci)

    def _submit_move(self, move_uci):
        if not self.tcp_client or not self.tcp_client.is_connected:
            self.show_chat_error("Mất kết nối tới phòng chơi.")
            return
        try:
            self._pending_invite_id = "requesting"
            self.tcp_client.send_message({
                "type": "move",
                "move": move_uci
            })
        except (ConnectionError, OSError) as exc:
            self._pending_invite_id = None
            self.show_chat_error(str(exc))
        finally:
            self.selected_square = None
            self._refresh_square_colors()

    def claim_draw(self):
        self._send_game_action("draw_claim")

    def resign(self):
        if messagebox.askyesno(
            "Xác nhận đầu hàng",
            "Bạn có chắc muốn đầu hàng ván này?",
            parent=self
        ):
            self._send_game_action("resign")

    def _send_game_action(self, action):
        if not self.tcp_client or not self.tcp_client.is_connected:
            self.show_chat_error("Mất kết nối tới phòng chơi.")
            return
        try:
            self.tcp_client.send_message({"type": action})
        except (ConnectionError, OSError) as exc:
            self.show_chat_error(str(exc))
    def send_chat(self):
        """Xử lý sự kiện bấm nút Gửi tin nhắn"""
        msg = self.chat_input.get().strip()
        if msg and self.tcp_client and self.tcp_client.is_connected:
            chat_payload = {
                "type": "chat",
                "sender": self.username,
                "message": msg
            }
            try:
                self.tcp_client.send_message(chat_payload)
                self.chat_input.delete(0, 'end')
            except (ConnectionError, OSError) as exc:
                self.show_chat_error(str(exc))

    def choose_chat_file(self):
        if self._file_upload_in_progress:
            return
        filename = filedialog.askopenfilename(
            parent=self,
            title="Chọn tệp gửi trong trận",
            filetypes=[
                (
                    "Tệp được hỗ trợ",
                    "*.jpg *.jpeg *.png *.gif *.webp *.mp4 *.mov *.webm "
                    "*.pdf *.doc *.docx *.wav *.mp3 *.m4a *.ogg"
                ),
                ("Tất cả tệp", "*.*")
            ]
        )
        if not filename:
            return
        try:
            upload = describe_upload(filename)
        except (OSError, ValueError) as exc:
            self.show_chat_error(str(exc))
            return
        if not self.tcp_client or not self.tcp_client.is_connected:
            self.show_chat_error("Chưa kết nối tới phòng chơi.")
            return

        self._file_upload_in_progress = True
        self.attach_file_btn.configure(state="disabled")
        self.chat_status_label.configure(text="Đang chuẩn bị tải tệp...")
        threading.Thread(
            target=self._upload_game_file,
            args=(filename, upload["resource_type"]),
            daemon=True
        ).start()

    def _upload_game_file(self, filename, resource_type):
        try:
            signature = self.tcp_client.request_cloudinary_signature_sync(
                resource_type
            )
            attachment = upload_file(filename, signature)
            self.after(0, self._send_game_file, attachment)
        except Exception as exc:
            try:
                self.after(0, self._finish_game_file_upload, str(exc))
            except TclError:
                print(f"[!] Tải tệp trong trận: {exc}")

    def _send_game_file(self, attachment):
        try:
            self.tcp_client.send_message({
                "type": "chat",
                "message": "",
                "attachment": attachment
            })
            self.chat_status_label.configure(text="Đã gửi tệp vào chat trận.")
        except (ConnectionError, OSError) as exc:
            self.chat_status_label.configure(
                text="Tệp đã tải lên nhưng không gửi được vào trận."
            )
            self.show_chat_error(str(exc))
        self._finish_game_file_upload()

    def _finish_game_file_upload(self, error=None):
        self._file_upload_in_progress = False
        if self.winfo_exists():
            self.attach_file_btn.configure(state="normal")
            if error:
                self.chat_status_label.configure(text="")
                self.show_chat_error(f"Không thể gửi tệp: {error}")

    def _open_chat_attachment(self, url):
        if (
            not isinstance(url, str)
            or not url.startswith(
                f"https://res.cloudinary.com/{CLOUDINARY_CLOUD_NAME}/"
            )
        ):
            self.show_chat_error("Đường dẫn tệp không hợp lệ.")
            return
        try:
            webbrowser.open(url)
        except OSError as exc:
            self.show_chat_error(f"Không thể mở tệp: {exc}")

    def _send_chat_on_enter(self, _event):
        self.send_chat()
        return "break"

    def open_emoji_picker(self):
        picker = ctk.CTkToplevel(self)
        picker.title("Chọn emoji")
        picker.resizable(False, False)
        picker.transient(self)
        for index, emoji in enumerate(("😀", "😂", "😊", "😍", "👍", "👏", "❤️", "🔥", "😢", "😮", "♟️", "🏆")):
            button = ctk.CTkButton(
                picker,
                text=emoji,
                width=48,
                height=42,
                font=("Segoe UI Emoji", 20),
                command=lambda value=emoji: self._insert_emoji(picker, value)
            )
            button.grid(row=index // 4, column=index % 4, padx=5, pady=5)

    def _insert_emoji(self, picker, emoji):
        self.chat_input.insert("insert", emoji)
        picker.destroy()
        self.chat_input.focus_set()

    def start_audio_recording(self):
        if not self.tcp_client or not self.tcp_client.is_connected:
            self.show_chat_error("Chưa kết nối tới phòng chơi.")
            return

        try:
            import sounddevice as sd
        except ImportError:
            self.show_chat_error(
                "Thiếu thư viện ghi âm. Cài đặt bằng lệnh: "
                "pip install -r chess_client/requirements.txt"
            )
            return

        with self._audio_lock:
            self._audio_frames.clear()
            self._audio_sample_count = 0

        def capture_audio(indata, frames, _time_info, _status):
            del _time_info, _status
            remaining = self._audio_sample_rate * self._max_record_seconds - self._audio_sample_count
            if remaining <= 0:
                return
            captured = min(frames, remaining)
            with self._audio_lock:
                self._audio_frames.append(indata[:captured].copy().tobytes())
                self._audio_sample_count += captured

        try:
            self._audio_stream = sd.InputStream(
                samplerate=self._audio_sample_rate,
                channels=1,
                dtype="int16",
                callback=capture_audio
            )
            self._audio_stream.start()
        except Exception as exc:
            self._audio_stream = None
            self.show_chat_error(f"Không thể mở micro: {exc}")
            return

        self._record_started_at = time.monotonic()
        self.record_btn.configure(state="disabled", text="Đang ghi...")
        self.stop_record_btn.configure(state="normal")
        self._check_recording_limit()

    def _check_recording_limit(self):
        if not self._audio_stream:
            return
        if time.monotonic() - self._record_started_at >= self._max_record_seconds:
            self.stop_and_send_recording()
            return
        self.after(200, self._check_recording_limit)

    def stop_and_send_recording(self):
        stream = self._audio_stream
        if not stream:
            return
        self._audio_stream = None

        try:
            stream.stop()
            stream.close()
        except Exception as exc:
            self.show_chat_error(f"Không thể dừng ghi âm: {exc}")
            self._set_recording_controls(False)
            return

        with self._audio_lock:
            audio_frames = b"".join(self._audio_frames)
            self._audio_frames.clear()
            self._audio_sample_count = 0
        self._set_recording_controls(False)
        if not audio_frames:
            self.show_chat_error("Không thu được âm thanh. Hãy kiểm tra micro.")
            return

        audio_file = io.BytesIO()
        with wave.open(audio_file, "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(self._audio_sample_rate)
            wav_file.writeframes(audio_frames)

        audio_data = audio_file.getvalue()
        if len(audio_data) > self._max_audio_bytes:
            self.show_chat_error("Bản ghi vượt quá giới hạn 10 giây.")
            return

        try:
            self.tcp_client.send_message({
                "type": "audio",
                "sender": self.username,
                "format": "wav",
                "audio": base64.b64encode(audio_data).decode("ascii")
            })
        except (ConnectionError, OSError) as exc:
            self.show_chat_error(str(exc))

    def _set_recording_controls(self, recording):
        self.record_btn.configure(
            state="disabled" if recording else "normal",
            text="Đang ghi..." if recording else "🎙 Ghi âm"
        )
        self.stop_record_btn.configure(state="normal" if recording else "disabled")

    def display_audio(self, sender, audio_data):
        self.after(0, self._add_audio_message, sender, audio_data)

    def _add_audio_message(self, sender, encoded_audio):
        self._clear_chat_empty_state()
        row = ctk.CTkFrame(self.chat_display, fg_color="transparent")
        row.pack(fill="x", pady=4, padx=6)
        bubble = ctk.CTkFrame(
            row,
            fg_color=("#E8F1FC", "#26384B") if sender == self.username else ("#F0F0F0", "#343434"),
            corner_radius=10
        )
        bubble.pack(anchor="e" if sender == self.username else "w", padx=4)
        ctk.CTkLabel(
            bubble,
            text=f"{sender} · ghi âm",
            font=("Roboto", 11, "bold"),
            anchor="w"
        ).pack(fill="x", padx=10, pady=(6, 2))
        ctk.CTkButton(
            bubble,
            text="▶ Phát tin nhắn thoại",
            width=190,
            command=lambda data=encoded_audio: self.play_audio_message(data)
        ).pack(padx=8, pady=(2, 8))
        self.chat_display.after(
            50, lambda: self.chat_display._parent_canvas.yview_moveto(1.0)
        )

    def play_audio_message(self, encoded_audio):
        if not isinstance(encoded_audio, str):
            self.show_chat_error("Tin nhắn thoại có định dạng không hợp lệ.")
            return
        try:
            audio_data = base64.b64decode(encoded_audio, validate=True)
            if len(audio_data) > self._max_audio_bytes:
                raise ValueError("Tin nhắn thoại vượt quá giới hạn cho phép.")
            with wave.open(io.BytesIO(audio_data), "rb") as wav_file:
                if (
                    wav_file.getnchannels() != 1
                    or wav_file.getsampwidth() != 2
                    or wav_file.getframerate() != self._audio_sample_rate
                    or wav_file.getnframes() > self._audio_sample_rate * self._max_record_seconds
                ):
                    raise ValueError("Định dạng tin nhắn thoại không được hỗ trợ.")
                frames = wav_file.readframes(wav_file.getnframes())
        except (TypeError, ValueError, wave.Error, EOFError, base64.binascii.Error) as exc:
            self.show_chat_error(f"Không thể phát tin nhắn thoại: {exc}")
            return

        threading.Thread(
            target=self._play_audio_data,
            args=(frames,),
            daemon=True
        ).start()

    def _play_audio_data(self, frames):
        try:
            import numpy as np
            import sounddevice as sd

            samples = np.frombuffer(frames, dtype=np.int16)
            sd.play(samples, samplerate=self._audio_sample_rate)
            sd.wait()
        except Exception as exc:
            self.show_chat_error(f"Không thể phát audio: {exc}")

    def show_chat_error(self, message):
        try:
            self.after(0, messagebox.showerror, "Chat", message)
        except TclError:
            print(f"[!] Chat: {message}")

    def on_closing(self):
        self._closing = True
        if self._call_client:
            self._finish_call_window(
                "Cuộc gọi đã kết thúc.",
                notify_server=True
            )
        if self._voice_connecting:
            self.voice_btn.configure(state="disabled")
        if self._voice_client:
            self._voice_client.stop()
            self._voice_client = None
        if self._audio_stream:
            try:
                self._audio_stream.stop()
                self._audio_stream.close()
            except Exception as exc:
                print(f"[!] Không thể dừng micro khi đóng cửa sổ: {exc}")
            self._audio_stream = None
        if self.tcp_client:
            self.tcp_client.set_room_state_callback(None)
            self.tcp_client.disconnect()
        self.destroy()
        if hasattr(self, "on_exit") and self.on_exit:
            self.on_exit()
        self.quit()
    def display_chat(self, sender, message, attachment=None):
        """Hứng tin nhắn từ thread mạng và đẩy về luồng UI"""
        self.after(0, self._insert_chat, sender, message, attachment)

    def _insert_chat(self, sender, message, attachment=None):
        self._clear_chat_empty_state()
        row = ctk.CTkFrame(self.chat_display, fg_color="transparent")
        row.pack(fill="x", pady=3, padx=6)
        bubble = ctk.CTkFrame(
            row,
            fg_color=("#E8F1FC", "#26384B") if sender == self.username else ("#F0F0F0", "#343434"),
            corner_radius=10
        )
        bubble.pack(anchor="e" if sender == self.username else "w", padx=4)
        ctk.CTkLabel(
            bubble,
            text=sender,
            font=("Roboto", 11, "bold"),
            anchor="w"
        ).pack(fill="x", padx=10, pady=(6, 0))
        ctk.CTkLabel(
            bubble,
            text=message or (
                "Đã gửi một tệp." if isinstance(attachment, dict) else ""
            ),
            wraplength=250,
            justify="left",
            anchor="w"
        ).pack(fill="x", padx=10, pady=(0, 7))
        if isinstance(attachment, dict):
            ctk.CTkButton(
                bubble,
                text=f"📎 {attachment.get('filename', 'Tệp đính kèm')}",
                width=220,
                command=lambda url=attachment.get("secure_url"): (
                    self._open_chat_attachment(url)
                )
            ).pack(fill="x", padx=8, pady=(0, 8))
        self.chat_display.after(
            50, lambda: self.chat_display._parent_canvas.yview_moveto(1.0)
        )

    def _clear_chat_empty_state(self):
        if not self._chat_has_messages:
            self.chat_empty_label.destroy()
            self._chat_has_messages = True