import threading
from tkinter import messagebox

import customtkinter as ctk

from auth_client import login_user, register_user


ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")


class LoginWindow(ctk.CTk):
    def __init__(self, on_login_success=None):
        super().__init__()
        self.on_login_success = on_login_success

        self.title("Chess Online")
        self.geometry("480x620")
        self.resizable(False, False)
        self.configure(fg_color="#0B1220")

        self.card = ctk.CTkFrame(
            self,
            width=380,
            height=500,
            corner_radius=22,
            fg_color="#111C2E",
            border_width=1,
            border_color="#23334D"
        )
        self.card.place(relx=0.5, rely=0.5, anchor="center")
        self.card.pack_propagate(False)

        ctk.CTkLabel(
            self.card,
            text="♞",
            font=("Segoe UI Symbol", 48),
            text_color="#73A7FF"
        ).pack(pady=(28, 0))
        ctk.CTkLabel(
            self.card,
            text="CHESS ONLINE",
            font=("Roboto", 25, "bold"),
            text_color="#F3F6FC"
        ).pack(pady=(2, 4))
        ctk.CTkLabel(
            self.card,
            text="Đăng nhập để vào bàn cờ cùng bạn bè",
            font=("Roboto", 13),
            text_color="#91A2BA"
        ).pack(pady=(0, 25))

        self.email_entry = ctk.CTkEntry(
            self.card,
            width=300,
            height=46,
            corner_radius=10,
            placeholder_text="Email",
            border_color="#344966"
        )
        self.email_entry.pack(pady=8)
        self.password_entry = ctk.CTkEntry(
            self.card,
            width=300,
            height=46,
            corner_radius=10,
            placeholder_text="Mật khẩu",
            show="*",
            border_color="#344966"
        )
        self.password_entry.pack(pady=8)
        self.password_entry.bind("<Return>", lambda _event: self.login_event())

        self.login_btn = ctk.CTkButton(
            self.card,
            width=300,
            height=46,
            corner_radius=10,
            text="Đăng nhập",
            font=("Roboto", 14, "bold"),
            command=self.login_event
        )
        self.login_btn.pack(pady=(22, 10))
        self.register_btn = ctk.CTkButton(
            self.card,
            width=300,
            height=42,
            corner_radius=10,
            text="Tạo tài khoản mới",
            fg_color="transparent",
            border_width=1,
            border_color="#344966",
            text_color="#C8D4E5",
            hover_color="#1B2A40",
            command=self.register_event
        )
        self.register_btn.pack(pady=5)

        self.status_label = ctk.CTkLabel(
            self.card,
            text="",
            font=("Roboto", 12),
            text_color="#91A2BA",
            wraplength=300
        )
        self.status_label.pack(pady=(16, 0))

    def _credentials(self):
        email = self.email_entry.get().strip()
        password = self.password_entry.get()
        if not email or not password:
            messagebox.showwarning(
                "Thiếu thông tin",
                "Vui lòng nhập email và mật khẩu.",
                parent=self
            )
            return None
        return email, password

    def _set_busy(self, busy, status=""):
        state = "disabled" if busy else "normal"
        self.login_btn.configure(
            state=state,
            text="Đang xử lý..." if busy else "Đăng nhập"
        )
        self.register_btn.configure(state=state)
        self.email_entry.configure(state=state)
        self.password_entry.configure(state=state)
        self.status_label.configure(text=status)

    def login_event(self):
        credentials = self._credentials()
        if not credentials:
            return
        self._set_busy(True, "Đang xác thực tài khoản...")
        threading.Thread(
            target=self._login_worker,
            args=credentials,
            daemon=True
        ).start()

    def _login_worker(self, email, password):
        try:
            user_data = login_user(email, password)
        except (ConnectionError, ValueError, KeyError) as exc:
            self.after(0, self._login_failed, str(exc))
            return
        username = user_data["email"].split("@", 1)[0]
        self.after(0, self._login_succeeded, username, user_data["idToken"])

    def _login_failed(self, message):
        self._set_busy(False)
        messagebox.showerror("Lỗi đăng nhập", message, parent=self)

    def _login_succeeded(self, username, id_token):
        if self.on_login_success:
            self.on_login_success(username, id_token)
        else:
            self._set_busy(False)

    def set_network_busy(self, busy, message=""):
        state = "disabled" if busy else "normal"
        self.login_btn.configure(state=state)
        self.register_btn.configure(state=state)
        self.email_entry.configure(state=state)
        self.password_entry.configure(state=state)
        self.status_label.configure(text=message)
        if not busy:
            self.login_btn.configure(text="Đăng nhập")

    def reset_for_login(self):
        self.email_entry.configure(state="normal")
        self.password_entry.configure(state="normal")
        self.login_btn.configure(state="normal", text="Đăng nhập")
        self.register_btn.configure(state="normal")
        self.password_entry.delete(0, "end")
        self.status_label.configure(text="")
        self.deiconify()
        self.lift()

    def register_event(self):
        credentials = self._credentials()
        if not credentials:
            return
        self._set_busy(True, "Đang tạo tài khoản...")
        threading.Thread(
            target=self._register_worker,
            args=credentials,
            daemon=True
        ).start()

    def _register_worker(self, email, password):
        try:
            register_user(email, password)
        except (ConnectionError, ValueError, KeyError) as exc:
            self.after(0, self._register_failed, str(exc))
            return
        self.after(0, self._register_succeeded)

    def _register_failed(self, message):
        self._set_busy(False)
        messagebox.showerror("Lỗi tạo tài khoản", message, parent=self)

    def _register_succeeded(self):
        self._set_busy(False, "Tài khoản đã tạo. Bạn có thể đăng nhập.")
        messagebox.showinfo(
            "Đăng ký thành công",
            "Tài khoản đã được tạo. Hãy đăng nhập để tiếp tục.",
            parent=self
        )


if __name__ == "__main__":
    app = LoginWindow()
    app.mainloop()
