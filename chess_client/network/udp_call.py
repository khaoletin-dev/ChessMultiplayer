import json
import queue
import socket
import struct
import threading
import time


CALL_AUDIO_PACKET_PREFIX = b"CV2"
CALL_VIDEO_PACKET_PREFIX = b"CV3"
SAMPLE_RATE = 16000
BLOCK_SIZE = 320
MAX_PACKET_BYTES = 1200
VIDEO_CHUNK_HEADER = struct.Struct("!IHH")
VIDEO_CHUNK_BYTES = MAX_PACKET_BYTES - len(CALL_VIDEO_PACKET_PREFIX) - VIDEO_CHUNK_HEADER.size
MAX_VIDEO_CHUNKS = 60
MAX_VIDEO_FRAME_BYTES = VIDEO_CHUNK_BYTES * MAX_VIDEO_CHUNKS


class UDPCallClient:
    def __init__(
        self,
        host,
        port,
        token,
        on_remote_video=None,
        on_local_video=None,
        on_error=None
    ):
        self.host = host
        self.port = port
        self.token = token
        self.on_remote_video = on_remote_video
        self.on_local_video = on_local_video
        self.on_error = on_error
        self._socket = None
        self._running = False
        self._receiver_thread = None
        self._camera_thread = None
        self._camera_enabled = threading.Event()
        self._input_stream = None
        self._output_stream = None
        self._playback_queue = queue.Queue(maxsize=60)
        self._pending_audio = bytearray()
        self._frame_chunks = {}
        self._frame_lock = threading.RLock()
        self._frame_id = 0
        self._numpy = None
        self._sounddevice = None
        self._error_reported = False
        self._error_lock = threading.Lock()

    def start(self):
        import numpy
        import sounddevice

        self._numpy = numpy
        self._sounddevice = sounddevice
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._socket.settimeout(0.75)
        registration = json.dumps({
            "type": "register",
            "token": self.token
        }).encode("utf-8")
        for _ in range(3):
            self._socket.sendto(registration, (self.host, self.port))
            try:
                response, _ = self._socket.recvfrom(256)
            except socket.timeout:
                continue
            if json.loads(response.decode("utf-8")).get("type") == "registered":
                break
        else:
            self._socket.close()
            self._socket = None
            raise ConnectionError("Không thể đăng ký cuộc gọi UDP với Game Node.")

        self._socket.settimeout(0.25)
        self._running = True
        try:
            self._output_stream = sounddevice.OutputStream(
                samplerate=SAMPLE_RATE,
                channels=1,
                dtype="int16",
                blocksize=BLOCK_SIZE,
                callback=self._play_audio
            )
            self._output_stream.start()
        except Exception:
            self.stop()
            raise

        self._receiver_thread = threading.Thread(
            target=self._receive_media,
            daemon=True
        )
        self._receiver_thread.start()

    def set_microphone_enabled(self, enabled):
        if enabled:
            if self._input_stream:
                return
            self._input_stream = self._sounddevice.InputStream(
                samplerate=SAMPLE_RATE,
                channels=1,
                dtype="int16",
                blocksize=BLOCK_SIZE,
                callback=self._send_audio
            )
            try:
                self._input_stream.start()
            except Exception:
                self._input_stream.close()
                self._input_stream = None
                raise
            return

        stream = self._input_stream
        self._input_stream = None
        if stream:
            stream.stop()
            stream.close()

    def set_camera_enabled(self, enabled):
        if enabled:
            if self._camera_thread and self._camera_thread.is_alive():
                return
            self._camera_enabled.set()
            self._camera_thread = threading.Thread(
                target=self._capture_camera,
                daemon=True
            )
            self._camera_thread.start()
            return

        self._camera_enabled.clear()
        thread = self._camera_thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=1)
        if not thread or not thread.is_alive():
            self._camera_thread = None

    def _send_audio(self, indata, _frames, _time_info, status):
        del _frames, _time_info
        if status:
            self._report_error(f"Trạng thái micro: {status}")
        udp_socket = self._socket
        if not self._running or not udp_socket:
            return
        packet = CALL_AUDIO_PACKET_PREFIX + indata.tobytes()
        if len(packet) > MAX_PACKET_BYTES:
            return
        try:
            udp_socket.sendto(packet, (self.host, self.port))
        except OSError as exc:
            if self._running:
                self._report_error(f"Không thể gửi âm thanh UDP: {exc}")

    def _capture_camera(self):
        try:
            import cv2
        except ImportError as exc:
            self._camera_enabled.clear()
            self._report_error(
                "Thiếu OpenCV. Cài đặt bằng lệnh: "
                "pip install -r chess_client/requirements.txt"
            )
            return

        camera = None
        try:
            camera = cv2.VideoCapture(0, cv2.CAP_DSHOW)
            if not camera.isOpened():
                self._report_error(
                    "Không thể mở camera. Hãy kiểm tra quyền truy cập."
                )
                return
            camera.set(cv2.CAP_PROP_FRAME_WIDTH, 320)
            camera.set(cv2.CAP_PROP_FRAME_HEIGHT, 240)
            camera.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            while self._running and self._camera_enabled.is_set():
                success, frame = camera.read()
                if not success:
                    self._report_error("Không nhận được hình ảnh từ camera.")
                    break
                frame = cv2.resize(frame, (320, 240))
                encoded, jpeg = cv2.imencode(
                    ".jpg",
                    frame,
                    [cv2.IMWRITE_JPEG_QUALITY, 42]
                )
                if not encoded:
                    continue
                frame_data = jpeg.tobytes()
                if not frame_data or len(frame_data) > MAX_VIDEO_FRAME_BYTES:
                    continue
                if self.on_local_video:
                    self.on_local_video(frame_data)
                self._send_video_frame(frame_data)
                time.sleep(0.12)
        except Exception as exc:
            self._report_error(f"Lỗi camera: {exc}")
        finally:
            if camera:
                camera.release()
            self._camera_enabled.clear()
            self._camera_thread = None

    def _send_video_frame(self, frame_data):
        udp_socket = self._socket
        if not self._running or not udp_socket:
            return
        chunk_count = (
            len(frame_data) + VIDEO_CHUNK_BYTES - 1
        ) // VIDEO_CHUNK_BYTES
        if not 1 <= chunk_count <= MAX_VIDEO_CHUNKS:
            return
        self._frame_id = (self._frame_id + 1) & 0xFFFFFFFF
        for index in range(chunk_count):
            start = index * VIDEO_CHUNK_BYTES
            chunk = frame_data[start:start + VIDEO_CHUNK_BYTES]
            packet = (
                CALL_VIDEO_PACKET_PREFIX
                + VIDEO_CHUNK_HEADER.pack(self._frame_id, index, chunk_count)
                + chunk
            )
            try:
                udp_socket.sendto(packet, (self.host, self.port))
            except OSError as exc:
                if self._running:
                    self._report_error(f"Không thể gửi video UDP: {exc}")
                return

    def _receive_media(self):
        udp_socket = self._socket
        while self._running and udp_socket:
            try:
                packet, _ = udp_socket.recvfrom(MAX_PACKET_BYTES + 1)
            except socket.timeout:
                self._discard_expired_frames()
                continue
            except OSError as exc:
                if self._running:
                    self._report_error(f"Không thể nhận media UDP: {exc}")
                break
            if not packet or len(packet) > MAX_PACKET_BYTES:
                continue
            if packet.startswith(CALL_AUDIO_PACKET_PREFIX):
                audio_data = packet[len(CALL_AUDIO_PACKET_PREFIX):]
                if audio_data and len(audio_data) % 2 == 0:
                    self._queue_audio(audio_data)
            elif packet.startswith(CALL_VIDEO_PACKET_PREFIX):
                self._receive_video_chunk(packet)

    def _queue_audio(self, audio_data):
        if self._playback_queue.full():
            try:
                self._playback_queue.get_nowait()
            except queue.Empty:
                pass
        try:
            self._playback_queue.put_nowait(audio_data)
        except queue.Full:
            pass

    def _receive_video_chunk(self, packet):
        header_start = len(CALL_VIDEO_PACKET_PREFIX)
        header_end = header_start + VIDEO_CHUNK_HEADER.size
        if len(packet) <= header_end:
            return
        frame_id, chunk_index, chunk_count = VIDEO_CHUNK_HEADER.unpack(
            packet[header_start:header_end]
        )
        if not 1 <= chunk_count <= MAX_VIDEO_CHUNKS or chunk_index >= chunk_count:
            return

        now = time.monotonic()
        chunk = packet[header_end:]
        frame_data = None
        with self._frame_lock:
            self._discard_expired_frames(now)
            frame = self._frame_chunks.get(frame_id)
            if frame is None:
                if len(self._frame_chunks) >= 3:
                    oldest_id = min(
                        self._frame_chunks,
                        key=lambda key: self._frame_chunks[key]["created"]
                    )
                    self._frame_chunks.pop(oldest_id, None)
                frame = {
                    "created": now,
                    "count": chunk_count,
                    "chunks": {}
                }
                self._frame_chunks[frame_id] = frame
            if frame["count"] != chunk_count:
                self._frame_chunks.pop(frame_id, None)
                return
            frame["chunks"].setdefault(chunk_index, chunk)
            if len(frame["chunks"]) == chunk_count:
                frame_data = b"".join(
                    frame["chunks"][index]
                    for index in range(chunk_count)
                )
                self._frame_chunks.pop(frame_id, None)
        if frame_data and len(frame_data) <= MAX_VIDEO_FRAME_BYTES:
            if self.on_remote_video:
                self.on_remote_video(frame_data)

    def _discard_expired_frames(self, now=None):
        now = now or time.monotonic()
        with self._frame_lock:
            expired = [
                frame_id
                for frame_id, frame in self._frame_chunks.items()
                if now - frame["created"] > 0.75
            ]
            for frame_id in expired:
                self._frame_chunks.pop(frame_id, None)

    def _play_audio(self, outdata, _frames, _time_info, _status):
        del _frames, _time_info, _status
        outdata.fill(0)
        required_bytes = outdata.nbytes
        while len(self._pending_audio) < required_bytes:
            try:
                self._pending_audio.extend(self._playback_queue.get_nowait())
            except queue.Empty:
                break
        available_bytes = min(len(self._pending_audio), required_bytes)
        available_bytes -= available_bytes % 2
        if available_bytes:
            samples = self._numpy.frombuffer(
                self._pending_audio[:available_bytes],
                dtype=self._numpy.int16
            )
            outdata[:len(samples), 0] = samples
            del self._pending_audio[:available_bytes]

    def _report_error(self, message):
        if not self.on_error:
            return
        with self._error_lock:
            if self._error_reported:
                return
            self._error_reported = True
        self.on_error(message)

    def stop(self):
        self._running = False
        self._camera_enabled.clear()
        camera_thread = self._camera_thread
        if camera_thread and camera_thread is not threading.current_thread():
            camera_thread.join(timeout=1)

        for stream in (self._input_stream, self._output_stream):
            if stream:
                try:
                    stream.stop()
                except Exception as exc:
                    self._report_error(f"Không thể dừng thiết bị gọi: {exc}")
                try:
                    stream.close()
                except Exception as exc:
                    self._report_error(f"Không thể đóng thiết bị gọi: {exc}")
        self._input_stream = None
        self._output_stream = None
        if self._socket:
            self._socket.close()
            self._socket = None
        thread = self._receiver_thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=1)
        self._receiver_thread = None
