import tempfile
import threading
import time
import wave
import webbrowser
from pathlib import Path
from tkinter import TclError, filedialog, messagebox

import customtkinter as ctk

from network.cloudinary_upload import (
    CLOUDINARY_CLOUD_NAME,
    describe_upload,
    upload_file
)

VOICE_SAMPLE_RATE = 16000
MAX_VOICE_NOTE_SECONDS = 30


class LobbyWindow(ctk.CTk):
    def __init__(self, username="Người chơi", on_logout=None, on_close=None):
        super().__init__()
        self.username = username
        self.on_logout = on_logout
        self.on_close = on_close
        self.tcp_client = None
        self.match_history = []
        self.profile_window = None
        self._lobby_chat_messages = []
        self._match_chat_dialogs = {}
        self._voice_stream = None
        self._voice_chunks = []
        self._voice_recording_started = 0.0
        self._voice_recording = False
        self._voice_stop_after_id = None
        self._voice_lock = threading.Lock()
        self._upload_in_progress = False

        self.title("Chess Online · Sảnh chờ")
        self.geometry("1180x740")
        self.minsize(1000, 640)
        self.configure(fg_color="#0B1220")
        self.protocol("WM_DELETE_WINDOW", self.on_closing)
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self.sidebar = ctk.CTkFrame(
            self,
            width=230,
            corner_radius=0,
            fg_color="#111C2E"
        )
        self.sidebar.grid(row=0, column=0, sticky="nsew")
        self.sidebar.grid_propagate(False)
        ctk.CTkLabel(
            self.sidebar,
            text="♞  CHESS",
            font=("Roboto", 23, "bold"),
            text_color="#73A7FF"
        ).pack(anchor="w", padx=24, pady=(28, 4))
        ctk.CTkLabel(
            self.sidebar,
            text="ONLINE",
            font=("Roboto", 11, "bold"),
            text_color="#91A2BA"
        ).pack(anchor="w", padx=61, pady=(0, 28))

        ctk.CTkLabel(
            self.sidebar,
            text="TẠO TRẬN ĐẤU",
            font=("Roboto", 11, "bold"),
            text_color="#91A2BA"
        ).pack(anchor="w", padx=22, pady=(8, 8))
        self.btn_1vs1 = ctk.CTkButton(
            self.sidebar,
            text="♟   Phòng 1 vs 1",
            height=44,
            anchor="w",
            fg_color="#1B2A40",
            hover_color="#263A56",
            command=self.create_1vs1
        )
        self.btn_1vs1.pack(fill="x", padx=14, pady=5)
        self.btn_2vs2 = ctk.CTkButton(
            self.sidebar,
            text="♟   Phòng đội 2 vs 2",
            height=44,
            anchor="w",
            fg_color="#1B2A40",
            hover_color="#263A56",
            command=self.create_2vs2
        )
        self.btn_2vs2.pack(fill="x", padx=14, pady=5)

        self.profile_card = ctk.CTkFrame(
            self.sidebar,
            fg_color="#17243A",
            corner_radius=12
        )
        self.profile_card.pack(side="bottom", fill="x", padx=14, pady=(8, 14))
        ctk.CTkLabel(
            self.profile_card,
            text="ĐANG ĐĂNG NHẬP",
            font=("Roboto", 10, "bold"),
            text_color="#91A2BA"
        ).pack(anchor="w", padx=12, pady=(12, 2))
        self.welcome_label = ctk.CTkLabel(
            self.profile_card,
            text=username,
            font=("Roboto", 14, "bold"),
            text_color="#F3F6FC",
            anchor="w"
        )
        self.welcome_label.pack(fill="x", padx=12, pady=(0, 10))
        self.btn_profile = ctk.CTkButton(
            self.profile_card,
            text="Hồ sơ trận đấu",
            height=36,
            fg_color="#263F66",
            hover_color="#335785",
            command=self.show_profile
        )
        self.btn_profile.pack(fill="x", padx=10, pady=(0, 8))
        self.btn_logout = ctk.CTkButton(
            self.profile_card,
            text="Đăng xuất",
            height=36,
            fg_color="#7B3442",
            hover_color="#963D4F",
            command=self.logout
        )
        self.btn_logout.pack(fill="x", padx=10, pady=(0, 10))

        self.content = ctk.CTkFrame(self, fg_color="transparent")
        self.content.grid(row=0, column=1, sticky="nsew", padx=28, pady=26)
        self.content.grid_columnconfigure(0, weight=1)
        self.content.grid_rowconfigure(1, weight=3)
        self.content.grid_rowconfigure(2, weight=2)

        self.header = ctk.CTkFrame(
            self.content,
            fg_color="#111C2E",
            corner_radius=16,
            border_width=1,
            border_color="#23334D"
        )
        self.header.grid(row=0, column=0, sticky="ew", pady=(0, 18))
        ctk.CTkLabel(
            self.header,
            text="Sẵn sàng cho ván tiếp theo?",
            font=("Roboto", 23, "bold"),
            text_color="#F3F6FC"
        ).pack(anchor="w", padx=22, pady=(18, 2))
        ctk.CTkLabel(
            self.header,
            text="Chọn một phòng đang chờ hoặc tạo trận mới.",
            font=("Roboto", 13),
            text_color="#91A2BA"
        ).pack(anchor="w", padx=22, pady=(0, 18))

        self.columns = ctk.CTkFrame(self.content, fg_color="transparent")
        self.columns.grid(row=1, column=0, sticky="nsew")
        self.columns.grid_columnconfigure(0, weight=1)
        self.columns.grid_columnconfigure(1, weight=0, minsize=250)
        self.columns.grid_rowconfigure(0, weight=1)

        self.rooms_panel = ctk.CTkFrame(
            self.columns,
            fg_color="#111C2E",
            corner_radius=16,
            border_width=1,
            border_color="#23334D"
        )
        self.rooms_panel.grid(row=0, column=0, sticky="nsew", padx=(0, 14))
        self.rooms_panel.grid_rowconfigure(1, weight=1)
        self.rooms_panel.grid_columnconfigure(0, weight=1)
        self.rooms_label = ctk.CTkLabel(
            self.rooms_panel,
            text="Phòng đang chờ",
            font=("Roboto", 17, "bold"),
            text_color="#F3F6FC"
        )
        self.rooms_label.grid(row=0, column=0, sticky="w", padx=18, pady=(17, 8))
        self.rooms_list = ctk.CTkScrollableFrame(
            self.rooms_panel,
            fg_color="transparent"
        )
        self.rooms_list.grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 10))

        self.online_frame = ctk.CTkFrame(
            self.columns,
            width=250,
            fg_color="#111C2E",
            corner_radius=16,
            border_width=1,
            border_color="#23334D"
        )
        self.online_frame.grid(row=0, column=1, sticky="nsew")
        self.online_frame.grid_propagate(False)
        self.online_frame.grid_rowconfigure(1, weight=1)
        ctk.CTkLabel(
            self.online_frame,
            text="Người chơi online",
            font=("Roboto", 16, "bold"),
            text_color="#F3F6FC"
        ).grid(row=0, column=0, sticky="w", padx=16, pady=(17, 8))
        self.online_list = ctk.CTkScrollableFrame(
            self.online_frame,
            width=220,
            fg_color="transparent"
        )
        self.online_list.grid(row=1, column=0, sticky="nsew", padx=8, pady=(0, 8))
        self._show_empty(self.rooms_list, "Chưa có phòng nào.\nTạo phòng mới để bắt đầu.")

        self.chat_panel = ctk.CTkFrame(
            self.content,
            fg_color="#111C2E",
            corner_radius=16,
            border_width=1,
            border_color="#23334D"
        )
        self.chat_panel.grid(row=2, column=0, sticky="nsew")
        self.chat_panel.grid_rowconfigure(1, weight=1)
        self.chat_panel.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            self.chat_panel,
            text="Chat sảnh · chia sẻ tin nhắn và bản ghi trận",
            font=("Roboto", 15, "bold"),
            text_color="#F3F6FC"
        ).grid(row=0, column=0, sticky="w", padx=16, pady=(10, 4))
        self.lobby_chat_display = ctk.CTkScrollableFrame(
            self.chat_panel,
            height=115,
            fg_color="#0E1929",
            corner_radius=10
        )
        self.lobby_chat_display.grid(
            row=1, column=0, sticky="nsew", padx=12, pady=(0, 7)
        )
        chat_controls = ctk.CTkFrame(self.chat_panel, fg_color="transparent")
        chat_controls.grid(row=2, column=0, sticky="ew", padx=12, pady=(0, 10))
        chat_controls.grid_columnconfigure(0, weight=1)
        self.lobby_chat_input = ctk.CTkEntry(
            chat_controls,
            height=38,
            placeholder_text="Viết tin nhắn sảnh..."
        )
        self.lobby_chat_input.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self.lobby_chat_input.bind("<Return>", self._send_lobby_chat_on_enter)
        self.chat_status_label = ctk.CTkLabel(
            chat_controls,
            text="",
            width=0,
            text_color="#91A2BA",
            font=("Roboto", 10)
        )
        self.chat_status_label.grid(row=1, column=0, columnspan=5, sticky="w")
        self.attach_file_button = ctk.CTkButton(
            chat_controls,
            text="📎",
            width=38,
            height=38,
            command=self.choose_chat_file
        )
        self.attach_file_button.grid(row=0, column=1, padx=(0, 5))
        self.emoji_button = ctk.CTkButton(
            chat_controls,
            text="😊",
            width=38,
            height=38,
            command=self.open_emoji_picker
        )
        self.emoji_button.grid(row=0, column=2, padx=(0, 5))
        self.voice_note_button = ctk.CTkButton(
            chat_controls,
            text="🎙",
            width=42,
            height=38,
            fg_color="#7B3442",
            hover_color="#963D4F",
            command=self.toggle_voice_note_recording
        )
        self.voice_note_button.grid(row=0, column=3, padx=(0, 5))
        self.send_chat_button = ctk.CTkButton(
            chat_controls,
            text="Gửi",
            width=62,
            height=38,
            command=self.send_lobby_message
        )
        self.send_chat_button.grid(row=0, column=4, padx=(0, 5))
        self.share_match_button = ctk.CTkButton(
            chat_controls,
            text="Chia sẻ ván gần nhất",
            width=140,
            height=38,
            fg_color="#263F66",
            hover_color="#335785",
            command=self.share_latest_match
        )
        self.share_match_button.grid(row=0, column=5)

    def _show_empty(self, parent, text):
        ctk.CTkLabel(
            parent,
            text=text,
            justify="center",
            text_color="#91A2BA",
            font=("Roboto", 13)
        ).pack(expand=True, pady=28)

    def add_online_user(self, username):
        frame = ctk.CTkFrame(self.online_list, fg_color="#17243A", corner_radius=8)
        frame.pack(fill="x", pady=4, padx=4)
        ctk.CTkLabel(
            frame,
            text=f"●  {username}",
            text_color="#9BD9AE",
            anchor="w"
        ).pack(side="left", padx=10, pady=9)

    def add_room_item(self, room_id, mode, host, players, capacity):
        frame = ctk.CTkFrame(self.rooms_list, fg_color="#17243A", corner_radius=10)
        frame.pack(fill="x", pady=5, padx=4)
        info = ctk.CTkFrame(frame, fg_color="transparent")
        info.pack(side="left", fill="x", expand=True, padx=13, pady=11)
        ctk.CTkLabel(
            info,
            text=f"{'Đội 2 vs 2' if mode == '2vs2' else 'Đấu 1 vs 1'}  ·  {players}/{capacity} người",
            font=("Roboto", 13, "bold"),
            text_color="#F3F6FC",
            anchor="w"
        ).pack(anchor="w")
        ctk.CTkLabel(
            info,
            text=f"Phòng {room_id}  ·  Chủ phòng: {host}",
            font=("Roboto", 11),
            text_color="#91A2BA",
            anchor="w"
        ).pack(anchor="w", pady=(3, 0))
        return frame

    def update_rooms_list(self, rooms_data):
        self.after(0, self._draw_rooms_list, rooms_data)

    def _draw_rooms_list(self, rooms_data):
        for widget in self.rooms_list.winfo_children():
            widget.destroy()
        if not rooms_data:
            self._show_empty(self.rooms_list, "Chưa có phòng nào.\nTạo phòng mới để bắt đầu.")
            return

        for room in rooms_data:
            token = room["token"]
            capacity = room.get("capacity", 4 if room["mode"] == "2vs2" else 2)
            frame = self.add_room_item(
                room_id=token,
                mode=room["mode"],
                host=room["host"],
                players=room.get("players", 1),
                capacity=capacity
            )
            if room.get("can_cancel", room.get("is_host", False)):
                text, color, hover, command = (
                    "Hủy phòng",
                    "#7B3442",
                    "#963D4F",
                    lambda room_token=token: self.cancel_room(room_token)
                )
            elif room.get("is_member"):
                text, color, hover, command = (
                    "Vào lại",
                    "#263F66",
                    "#335785",
                    lambda room_token=token: self.join_existing_room(room_token)
                )
            elif not room.get("can_join", True):
                text, color, hover, command = (
                    "Đủ người",
                    "#344257",
                    "#344257",
                    None
                )
            else:
                text, color, hover, command = (
                    "Tham gia",
                    "#28694A",
                    "#32855D",
                    lambda room_token=token: self.join_existing_room(room_token)
                )
            ctk.CTkButton(
                frame,
                text=text,
                width=92,
                height=36,
                fg_color=color,
                hover_color=hover,
                command=command,
                state="disabled" if command is None else "normal"
            ).pack(side="right", padx=12, pady=10)

    def join_existing_room(self, token):
        self._send_lobby_action({"action": "join_room_request", "token": token})

    def cancel_room(self, token):
        self._send_lobby_action({"action": "cancel_room", "token": token})

    def create_1vs1(self):
        self._create_room("1vs1")

    def create_2vs2(self):
        self._create_room("2vs2")

    def _create_room(self, mode):
        self._send_lobby_action({"action": "create_room", "mode": mode})

    def _send_lobby_action(self, payload):
        if not self.tcp_client or not self.tcp_client.is_connected:
            self.show_server_error("Chưa kết nối tới máy chủ.")
            return
        try:
            self.tcp_client.send_message(payload)
        except (ConnectionError, OSError) as exc:
            self.show_server_error(str(exc))

    def show_server_error(self, message):
        try:
            self.after(0, messagebox.showerror, "Sảnh chờ", message)
        except Exception as exc:
            print(f"[!] Sảnh chờ: {exc}")

    def update_online_users(self, users_data):
        self.after(0, self._draw_online_users, users_data)

    def _draw_online_users(self, users_data):
        for widget in self.online_list.winfo_children():
            widget.destroy()
        if not users_data:
            self._show_empty(self.online_list, "Chưa có người chơi online.")
            return

        for user in users_data:
            username = user.get("username", "Unknown")
            rank = user.get("rank_points", 1000)
            frame = ctk.CTkFrame(self.online_list, fg_color="#17243A", corner_radius=8)
            frame.pack(fill="x", pady=4, padx=4)
            ctk.CTkLabel(
                frame,
                text=f"●  {username}",
                text_color="#9BD9AE",
                anchor="w"
            ).pack(fill="x", padx=10, pady=(8, 0))
            ctk.CTkLabel(
                frame,
                text=f"♟  {rank} điểm",
                text_color="#91A2BA",
                anchor="w",
                font=("Roboto", 11)
            ).pack(fill="x", padx=10, pady=(0, 8))

    def update_match_history(self, records):
        self.after(0, self._set_match_history, records)

    def _set_match_history(self, records):
        self.match_history = records if isinstance(records, list) else []
        if self.profile_window and self.profile_window.winfo_exists():
            self._draw_match_history()

    def show_profile(self):
        if not self.tcp_client or not self.tcp_client.is_connected:
            self.show_server_error("Chưa kết nối tới máy chủ.")
            return
        try:
            self.tcp_client.get_match_history()
        except (ConnectionError, OSError) as exc:
            self.show_server_error(str(exc))

        if self.profile_window and self.profile_window.winfo_exists():
            self.profile_window.focus()
            return

        self.profile_window = ctk.CTkToplevel(self)
        self.profile_window.title(f"Hồ sơ trận đấu · {self.username}")
        self.profile_window.geometry("680x600")
        self.profile_window.minsize(560, 420)
        self.profile_window.transient(self)
        self.profile_window.grid_columnconfigure(0, weight=1)
        self.profile_window.grid_rowconfigure(1, weight=1)
        ctk.CTkLabel(
            self.profile_window,
            text=f"Lịch sử trận đấu của {self.username}",
            font=("Roboto", 21, "bold"),
            text_color="#F3F6FC"
        ).grid(row=0, column=0, sticky="w", padx=20, pady=(18, 12))
        self.profile_history_list = ctk.CTkScrollableFrame(
            self.profile_window,
            fg_color="#111C2E",
            corner_radius=12
        )
        self.profile_history_list.grid(
            row=1, column=0, sticky="nsew", padx=16, pady=(0, 16)
        )
        self._draw_match_history()

    def _draw_match_history(self):
        for widget in self.profile_history_list.winfo_children():
            widget.destroy()
        if not self.match_history:
            self._show_empty(
                self.profile_history_list,
                "Chưa có trận nào được lưu.\nHoàn thành một trận để tạo hồ sơ."
            )
            return

        for record in self.match_history:
            item = ctk.CTkFrame(
                self.profile_history_list,
                fg_color="#17243A",
                corner_radius=10
            )
            item.pack(fill="x", padx=5, pady=5)
            details = ctk.CTkFrame(item, fg_color="transparent")
            details.pack(side="left", fill="x", expand=True, padx=12, pady=10)
            ctk.CTkLabel(
                details,
                text=self._record_title(record),
                font=("Roboto", 13, "bold"),
                text_color="#F3F6FC",
                anchor="w"
            ).pack(anchor="w")
            ctk.CTkLabel(
                details,
                text=self._record_players_text(record),
                font=("Roboto", 11),
                text_color="#91A2BA",
                anchor="w"
            ).pack(anchor="w", pady=(3, 0))
            actions = ctk.CTkFrame(item, fg_color="transparent")
            actions.pack(side="right", padx=10)
            ctk.CTkButton(
                actions,
                text="Nước đi",
                width=78,
                command=lambda saved=record: self.view_match_record(saved)
            ).pack(pady=3)
            ctk.CTkButton(
                actions,
                text="Chia sẻ",
                width=78,
                fg_color="#28694A",
                hover_color="#32855D",
                command=lambda saved=record: self.share_match(saved)
            ).pack(pady=3)
            ctk.CTkButton(
                actions,
                text="Chat trận",
                width=78,
                command=lambda saved=record: self.load_match_chat(saved)
            ).pack(pady=3)

    def _record_title(self, record):
        mode = "2 vs 2" if record.get("mode") == "2vs2" else "1 vs 1"
        result = record.get("result", "—")
        completed_at = record.get("completed_at", "")
        date_text = completed_at.replace("T", " ")[:16] if completed_at else ""
        move_count = record.get("moves_count", len(record.get("moves", [])))
        return f"{mode} · {result} · {move_count} nước · {date_text}"

    @staticmethod
    def _record_players_text(record):
        players = record.get("players", [])
        return "  |  ".join(
            f"{player.get('username', 'Người chơi')} ({'Trắng' if player.get('team') == 'white' else 'Đen'})"
            for player in players
        )

    def view_match_record(self, record):
        dialog = ctk.CTkToplevel(self)
        dialog.title(f"Biên bản trận · {record.get('match_id', '')}")
        dialog.geometry("620x560")
        dialog.transient(self)
        dialog.grid_columnconfigure(0, weight=1)
        dialog.grid_rowconfigure(1, weight=1)
        ctk.CTkLabel(
            dialog,
            text=self._record_title(record),
            font=("Roboto", 16, "bold"),
            wraplength=570
        ).grid(row=0, column=0, sticky="w", padx=16, pady=(16, 5))
        moves_text = self._format_moves(record.get("moves", []))
        moves_box = ctk.CTkTextbox(dialog, wrap="word")
        moves_box.grid(row=1, column=0, sticky="nsew", padx=16, pady=8)
        moves_box.insert("1.0", moves_text)
        moves_box.configure(state="disabled")
        ctk.CTkLabel(
            dialog,
            text=f"Kết thúc: {record.get('reason', '')}",
            wraplength=570,
            justify="left"
        ).grid(row=2, column=0, sticky="w", padx=16, pady=(0, 16))

    def load_match_chat(self, record):
        match_id = record.get("match_id")
        if not match_id:
            self.show_server_error("Bản ghi trận thiếu mã trận.")
            return
        try:
            self.tcp_client.get_match_chat(match_id)
        except (ConnectionError, OSError) as exc:
            self.show_server_error(str(exc))

    def display_match_chat_history(self, match_id, messages):
        self.after(0, self._show_match_chat_history, match_id, messages)

    def load_shared_match_record(self, match_id):
        try:
            self.tcp_client.get_shared_match_record(match_id)
        except (ConnectionError, OSError) as exc:
            self.show_server_error(str(exc))

    def display_shared_match_record(self, record):
        self.after(0, self.view_match_record, record)

    def _show_match_chat_history(self, match_id, messages):
        dialog = self._match_chat_dialogs.get(match_id)
        if dialog and dialog.winfo_exists():
            dialog.destroy()
        dialog = ctk.CTkToplevel(self)
        dialog.title(f"Chat trận · {match_id}")
        dialog.geometry("620x520")
        dialog.transient(self)
        dialog.grid_columnconfigure(0, weight=1)
        dialog.grid_rowconfigure(0, weight=1)
        self._match_chat_dialogs[match_id] = dialog
        chat_list = ctk.CTkScrollableFrame(
            dialog,
            fg_color="#0E1929",
            corner_radius=10
        )
        chat_list.grid(row=0, column=0, sticky="nsew", padx=14, pady=14)
        if not messages:
            self._show_empty(chat_list, "Trận này chưa có tin nhắn được lưu.")
        for message in messages:
            self._render_chat_message(chat_list, message)

    @staticmethod
    def _format_moves(moves):
        if not moves:
            return "Trận đấu chưa có nước đi."
        lines = []
        for index, move in enumerate(moves):
            ply = move.get("ply", index + 1)
            notation = move.get("san", move.get("uci", ""))
            if ply % 2:
                lines.append(f"{(ply + 1) // 2}. {notation}")
            else:
                lines[-1] += f"  {notation}"
        return "\n".join(lines)

    def share_latest_match(self):
        if not self.match_history:
            self.show_server_error("Bạn chưa có hồ sơ trận để chia sẻ.")
            return
        self.share_match(self.match_history[0])

    def share_match(self, record):
        match_id = record.get("match_id")
        if not match_id:
            self.show_server_error("Bản ghi trận thiếu mã trận.")
            return
        try:
            self.tcp_client.send_lobby_chat(
                f"Đã chia sẻ bản ghi trận: {self._record_title(record)}",
                match_id
            )
        except (ConnectionError, OSError) as exc:
            self.show_server_error(str(exc))

    def _send_lobby_chat_on_enter(self, _event):
        self.send_lobby_message()
        return "break"

    def send_lobby_message(self):
        text = self.lobby_chat_input.get().strip()
        if not text:
            return
        if not self.tcp_client or not self.tcp_client.is_connected:
            self.show_server_error("Chưa kết nối tới máy chủ.")
            return
        try:
            self.tcp_client.send_lobby_chat(text)
            self.lobby_chat_input.delete(0, "end")
        except (ConnectionError, OSError) as exc:
            self.show_server_error(str(exc))

    def display_lobby_chat(self, message):
        self.after(0, self._append_lobby_chat, message)

    def update_lobby_chat_history(self, messages):
        self.after(0, self._set_lobby_chat_history, messages)

    def _set_lobby_chat_history(self, messages):
        history = messages if isinstance(messages, list) else []
        history_ids = {
            message.get("message_id")
            for message in history
            if message.get("message_id")
        }
        history.extend(
            message
            for message in self._lobby_chat_messages
            if message.get("message_id") not in history_ids
        )
        self._lobby_chat_messages = history
        for widget in self.lobby_chat_display.winfo_children():
            widget.destroy()
        for message in self._lobby_chat_messages:
            self._render_chat_message(self.lobby_chat_display, message)
        if self._lobby_chat_messages:
            self.lobby_chat_display.after(
                50, lambda: self.lobby_chat_display._parent_canvas.yview_moveto(1.0)
            )

    def _append_lobby_chat(self, message):
        message_id = message.get("message_id")
        if message_id and any(
            previous.get("message_id") == message_id
            for previous in self._lobby_chat_messages
        ):
            return
        self._lobby_chat_messages.append(message)
        self._render_chat_message(self.lobby_chat_display, message)
        self.lobby_chat_display.after(
            50, lambda: self.lobby_chat_display._parent_canvas.yview_moveto(1.0)
        )

    def _render_chat_message(self, parent, message):
        sender = message.get("sender", "Người chơi")
        row = ctk.CTkFrame(parent, fg_color="#17243A", corner_radius=8)
        row.pack(fill="x", padx=4, pady=4)
        ctk.CTkLabel(
            row,
            text=f"{sender}: {message.get('message', '')}",
            anchor="w",
            justify="left",
            wraplength=780,
            text_color="#F3F6FC"
        ).pack(fill="x", padx=10, pady=(7, 3))
        record = message.get("record") or message.get("record_summary")
        if isinstance(record, dict):
            detail = ctk.CTkFrame(row, fg_color="#20324B", corner_radius=7)
            detail.pack(fill="x", padx=8, pady=(2, 8))
            ctk.CTkLabel(
                detail,
                text=f"{self._record_title(record)}\n{self._record_players_text(record)}",
                anchor="w",
                justify="left",
                wraplength=750,
                text_color="#B9C9DF"
            ).pack(side="left", fill="x", expand=True, padx=9, pady=7)
            ctk.CTkButton(
                detail,
                text="Xem biên bản",
                width=105,
                command=lambda match_id=record.get("match_id"): self.load_shared_match_record(match_id)
            ).pack(side="right", padx=8, pady=6)
        attachment = message.get("attachment")
        if isinstance(attachment, dict):
            attachment_row = ctk.CTkFrame(
                row,
                fg_color="#20324B",
                corner_radius=7
            )
            attachment_row.pack(fill="x", padx=8, pady=(2, 8))
            size_mb = attachment.get("bytes", 0) / (1024 * 1024)
            ctk.CTkLabel(
                attachment_row,
                text=(
                    f"{self._attachment_icon(attachment.get('category'))} "
                    f"{attachment.get('filename', 'Tệp')} · {size_mb:.1f} MB"
                ),
                anchor="w",
                text_color="#B9C9DF"
            ).pack(side="left", fill="x", expand=True, padx=9, pady=7)
            ctk.CTkButton(
                attachment_row,
                text="Mở tệp",
                width=78,
                command=lambda url=attachment.get("secure_url"): self._open_attachment(url)
            ).pack(side="right", padx=8, pady=6)

    @staticmethod
    def _attachment_icon(category):
        return {
            "image": "🖼",
            "video": "🎬",
            "audio": "🎙",
            "document": "📄"
        }.get(category, "📎")

    def _open_attachment(self, url):
        if (
            not isinstance(url, str)
            or not url.startswith(
                f"https://res.cloudinary.com/{CLOUDINARY_CLOUD_NAME}/"
            )
        ):
            self.show_server_error("Đường dẫn tệp không hợp lệ.")
            return
        try:
            webbrowser.open(url)
        except OSError as exc:
            self.show_server_error(f"Không thể mở tệp: {exc}")

    def choose_chat_file(self):
        if self._upload_in_progress:
            return
        filename = filedialog.askopenfilename(
            parent=self,
            title="Chọn tệp gửi vào chat sảnh",
            filetypes=[
                ("Tệp được hỗ trợ", "*.jpg *.jpeg *.png *.gif *.webp *.mp4 *.mov *.webm *.pdf *.doc *.docx *.wav *.mp3 *.m4a *.ogg"),
                ("Tất cả tệp", "*.*")
            ]
        )
        if filename:
            self._request_file_upload(filename, "")

    def open_emoji_picker(self):
        picker = ctk.CTkToplevel(self)
        picker.title("Chọn cảm xúc")
        picker.geometry("300x180")
        picker.resizable(False, False)
        picker.transient(self)
        emojis = ("😀", "😂", "😊", "😍", "🤔", "😮", "😢", "😎", "👍", "👏", "🔥", "❤️")
        for index, emoji in enumerate(emojis):
            button = ctk.CTkButton(
                picker,
                text=emoji,
                width=42,
                height=42,
                font=("Segoe UI Emoji", 20),
                command=lambda selected=emoji: self._insert_emoji(picker, selected)
            )
            button.grid(row=index // 6, column=index % 6, padx=4, pady=5)

    def _insert_emoji(self, picker, emoji):
        self.lobby_chat_input.insert("insert", emoji)
        picker.destroy()
        self.lobby_chat_input.focus_set()

    def toggle_voice_note_recording(self):
        if self._voice_recording:
            self.stop_voice_note_recording()
            return
        if self._upload_in_progress:
            return
        try:
            import sounddevice as sd

            self._voice_chunks = []
            self._voice_recording_started = time.monotonic()
            self._voice_recording = True
            self._voice_stream = sd.InputStream(
                samplerate=VOICE_SAMPLE_RATE,
                channels=1,
                dtype="int16",
                callback=self._capture_voice_chunk
            )
            self._voice_stream.start()
            self.voice_note_button.configure(
                text="■",
                fg_color="#C62828",
                hover_color="#B71C1C"
            )
            self.chat_status_label.configure(
                text=f"Đang ghi âm · tối đa {MAX_VOICE_NOTE_SECONDS} giây"
            )
            self._voice_stop_after_id = self.after(
                MAX_VOICE_NOTE_SECONDS * 1000,
                self._stop_voice_note_if_active
            )
        except Exception as exc:
            self._voice_recording = False
            if self._voice_stream:
                try:
                    self._voice_stream.close()
                except Exception as close_exc:
                    print(f"[!] Không thể đóng luồng ghi âm: {close_exc}")
                self._voice_stream = None
            self.show_server_error(f"Không thể bắt đầu ghi âm: {exc}")

    def _capture_voice_chunk(self, indata, _frames, _time_info, status):
        if status:
            print(f"[!] Voice note: {status}")
        if not self._voice_recording:
            return
        with self._voice_lock:
            self._voice_chunks.append(indata.copy().tobytes())

    def _stop_voice_note_if_active(self):
        if self._voice_recording:
            self.stop_voice_note_recording()

    def stop_voice_note_recording(self):
        if not self._voice_recording:
            return
        self._voice_recording = False
        if self._voice_stop_after_id:
            try:
                self.after_cancel(self._voice_stop_after_id)
            except TclError:
                pass
            self._voice_stop_after_id = None
        stream = self._voice_stream
        self._voice_stream = None
        if stream:
            try:
                stream.stop()
                stream.close()
            except Exception as exc:
                self.show_server_error(f"Không thể dừng ghi âm: {exc}")
        self.voice_note_button.configure(
            text="🎙",
            fg_color="#7B3442",
            hover_color="#963D4F"
        )
        with self._voice_lock:
            audio_data = b"".join(self._voice_chunks)
            self._voice_chunks.clear()
        if not audio_data:
            self.chat_status_label.configure(text="Không thu được âm thanh.")
            return

        temp_file = tempfile.NamedTemporaryFile(
            prefix="chess-voice-",
            suffix=".wav",
            delete=False
        )
        temp_path = temp_file.name
        temp_file.close()
        try:
            with wave.open(temp_path, "wb") as audio_file:
                audio_file.setnchannels(1)
                audio_file.setsampwidth(2)
                audio_file.setframerate(VOICE_SAMPLE_RATE)
                audio_file.writeframes(audio_data)
            self._request_file_upload(
                temp_path,
                "🎙 Ghi âm",
                cleanup_after_upload=True
            )
        except Exception as exc:
            Path(temp_path).unlink(missing_ok=True)
            self.show_server_error(f"Không thể chuẩn bị tệp ghi âm: {exc}")

    def _request_file_upload(self, path, caption, cleanup_after_upload=False):
        if not self.tcp_client or not self.tcp_client.is_connected:
            if cleanup_after_upload:
                Path(path).unlink(missing_ok=True)
            self.show_server_error("Chưa kết nối tới máy chủ.")
            return
        try:
            upload = describe_upload(path)
        except (OSError, ValueError) as exc:
            if cleanup_after_upload:
                Path(path).unlink(missing_ok=True)
            self.show_server_error(str(exc))
            return
        self._upload_in_progress = True
        self.attach_file_button.configure(state="disabled")
        self.voice_note_button.configure(state="disabled")
        self.chat_status_label.configure(
            text=f"Đang chuẩn bị tải {upload['filename']}..."
        )

        def signature_ready(response):
            try:
                self.after(
                    0,
                    self._upload_with_signature,
                    path,
                    caption,
                    cleanup_after_upload,
                    response
                )
            except Exception:
                if cleanup_after_upload:
                    Path(path).unlink(missing_ok=True)

        try:
            self.tcp_client.request_cloudinary_signature(
                upload["resource_type"],
                signature_ready
            )
        except (ConnectionError, OSError) as exc:
            self._finish_upload_error(str(exc), path, cleanup_after_upload)

    def _upload_with_signature(self, path, caption, cleanup_after_upload, signature):
        if signature.get("status") == "error":
            self._finish_upload_error(
                signature.get("message", "Không thể cấp quyền tải tệp."),
                path,
                cleanup_after_upload
            )
            return
        self.chat_status_label.configure(text="Đang tải tệp lên Cloudinary...")
        threading.Thread(
            target=self._upload_worker,
            args=(path, caption, cleanup_after_upload, signature),
            daemon=True
        ).start()

    def _upload_worker(self, path, caption, cleanup_after_upload, signature):
        try:
            attachment = upload_file(path, signature)
            self.after(0, self._finish_file_upload, caption, attachment)
        except Exception as exc:
            try:
                self.after(0, self._finish_upload_error, str(exc), path, False)
            except Exception:
                print(f"[!] Cloudinary upload: {exc}")
        finally:
            if cleanup_after_upload:
                Path(path).unlink(missing_ok=True)

    def _finish_file_upload(self, caption, attachment):
        try:
            self.tcp_client.send_lobby_chat(caption, attachment=attachment)
        except (ConnectionError, OSError) as exc:
            self.show_server_error(f"Tệp đã tải lên nhưng không gửi được vào chat: {exc}")
        self._upload_in_progress = False
        self.attach_file_button.configure(state="normal")
        self.voice_note_button.configure(state="normal")
        self.chat_status_label.configure(text="")

    def _finish_upload_error(self, message, path=None, cleanup_file=False):
        if cleanup_file and path:
            Path(path).unlink(missing_ok=True)
        self._upload_in_progress = False
        self.attach_file_button.configure(state="normal")
        self.voice_note_button.configure(state="normal")
        self.chat_status_label.configure(text="")
        self.show_server_error(f"Tải tệp thất bại: {message}")

    def logout(self):
        self._discard_voice_note_recording()
        if self.tcp_client:
            self.tcp_client.disconnect()
        if self.on_logout:
            self.on_logout()
        self.destroy()

    def on_closing(self):
        self._discard_voice_note_recording()
        if self.tcp_client:
            self.tcp_client.disconnect()
        if self.on_close:
            self.on_close()
        self.destroy()
        self.quit()

    def _discard_voice_note_recording(self):
        self._voice_recording = False
        if self._voice_stop_after_id:
            try:
                self.after_cancel(self._voice_stop_after_id)
            except TclError:
                pass
            self._voice_stop_after_id = None
        stream = self._voice_stream
        self._voice_stream = None
        if stream:
            try:
                stream.stop()
                stream.close()
            except Exception as exc:
                print(f"[!] Không thể dừng ghi âm khi đóng sảnh: {exc}")
        with self._voice_lock:
            self._voice_chunks.clear()


if __name__ == "__main__":
    app = LobbyWindow(username="Khoa")
    app.mainloop()
