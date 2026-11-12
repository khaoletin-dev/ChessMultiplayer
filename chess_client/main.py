import threading

import customtkinter as ctk
from tkinter import messagebox
from ui.login_ui import LoginWindow
from ui.lobby_ui import LobbyWindow
from network.tcp_client import ChessTCPClient
from ui.board_ui import BoardWindow

class ChessApp:
    def __init__(self):
        # Biến lưu trữ thông tin người dùng sau khi đăng nhập
        self.current_user = None
        self.id_token = None
        self._returning_to_lobby = False
        self.tcp_client = ChessTCPClient()
    def start(self):
        """Khởi chạy ứng dụng với màn hình Login"""
        self.show_login()

    def show_login(self):
        # Khởi tạo cửa sổ và lưu vào biến của Class
        self.login_window = LoginWindow(on_login_success=self.handle_login_success)
        self.login_window.mainloop() 

    def handle_login_success(self, username, id_token):
        print(f"[*] Firebase duyệt thành công cho user: {username}")
        self.id_token = id_token

        self.login_window.set_network_busy(True, "Đang kết nối máy chủ trò chơi...")
        threading.Thread(
            target=self._connect_game_server,
            args=(username, id_token),
            daemon=True
        ).start()

    def _connect_game_server(self, username, id_token):
        connected = self.tcp_client.connect_and_login(id_token)
        try:
            self.login_window.after(
                0,
                self._finish_login,
                username,
                connected,
                self.tcp_client.last_error
            )
        except Exception as exc:
            print(f"[!] Không thể cập nhật giao diện đăng nhập: {exc}")

    def _finish_login(self, username, connected, error):
        if not self.login_window.winfo_exists():
            return
        if connected:
            profile = self.tcp_client.user_data
            self.current_user = profile.get("username", username)
            self.login_window.withdraw()
            self.show_lobby()
        else:
            self.login_window.set_network_busy(False)
            messagebox.showerror(
                "Lỗi mạng",
                error or "Không thể kết nối đến Game Server.",
                parent=self.login_window
            )

    def show_lobby(self):
        lobby_window = LobbyWindow(
            username=self.current_user,
            on_logout=self.handle_logout,
            on_close=self.login_window.destroy
        )
        self.lobby_window = lobby_window
        lobby_window.tcp_client = self.tcp_client
        self.tcp_client.set_online_list_callback(lobby_window.update_online_users)
        self.tcp_client.set_rooms_list_callback(lobby_window.update_rooms_list)
        self.tcp_client.on_match_history_update = lobby_window.update_match_history
        self.tcp_client.on_lobby_chat_received = lobby_window.display_lobby_chat
        self.tcp_client.on_lobby_chat_history = lobby_window.update_lobby_chat_history
        self.tcp_client.on_match_chat_history = lobby_window.display_match_chat_history
        self.tcp_client.on_shared_match_record = lobby_window.display_shared_match_record
        self.tcp_client.on_server_error = lobby_window.show_server_error
        self.tcp_client.on_join_game_success = self.handle_join_game
        try:
            self.tcp_client.get_match_history()
            self.tcp_client.get_lobby_chat_history()
        except (ConnectionError, OSError) as exc:
            lobby_window.show_server_error(str(exc))
        lobby_window.mainloop()

    def handle_logout(self):
        self.current_user = None
        self.id_token = None
        self.login_window.reset_for_login()

    def handle_join_game(self, room_token, player_info=None):
        print(f"[*] Luồng Main nhận được tín hiệu: Chuẩn bị mở bàn cờ cho phòng {room_token}")
        self.login_window.after(0, self.show_board, room_token, player_info)

    def show_board(self, room_token, player_info=None):
        if hasattr(self, 'lobby_window') and self.lobby_window.winfo_exists():
            self.lobby_window.destroy()
        self._returning_to_lobby = False
        self.board_window = BoardWindow(
            room_token=room_token, 
            tcp_client=self.tcp_client, 
            username=self.current_user,
            player_info=player_info
        )
        self.board_window.on_exit = self.return_to_lobby
        self.board_window.mainloop()        

    def return_to_lobby(self):
        if self._returning_to_lobby:
            return
        self._returning_to_lobby = True
        try:
            self.login_window.after(0, self._reconnect_to_lobby)
        except Exception as exc:
            print(f"[!] Không thể quay lại sảnh: {exc}")

    def _reconnect_to_lobby(self):
        self.login_window.withdraw()
        self.login_window.set_network_busy(True, "Đang kết nối lại sảnh...")
        threading.Thread(
            target=self._reconnect_to_lobby_worker,
            daemon=True
        ).start()

    def _reconnect_to_lobby_worker(self):
        connected = bool(self.id_token) and self.tcp_client.connect_and_login(
            self.id_token
        )
        error = self.tcp_client.last_error
        try:
            self.login_window.after(
                0,
                self._finish_return_to_lobby,
                connected,
                error
            )
        except Exception as exc:
            print(f"[!] Không thể mở lại sảnh: {exc}")

    def _finish_return_to_lobby(self, connected, error):
        if not self.login_window.winfo_exists():
            return
        if connected:
            self._returning_to_lobby = False
            self.show_lobby()
            return
        self.id_token = None
        self._returning_to_lobby = False
        self.login_window.reset_for_login()
        messagebox.showerror(
            "Không thể vào sảnh",
            error or "Phiên đăng nhập hết hạn. Vui lòng đăng nhập lại.",
            parent=self.login_window
        )
if __name__ == "__main__":
    app = ChessApp()
    app.start()