import json
import queue
import socket
import threading


VOICE_PACKET_PREFIX = b"CV1"
SAMPLE_RATE = 16000
BLOCK_SIZE = 320
MAX_PACKET_BYTES = 1200


class UDPVoiceClient:
    def __init__(self, host, port, token, on_error=None):
        self.host = host
        self.port = port
        self.token = token
        self.on_error = on_error
        self._socket = None
        self._running = False
        self._receiver_thread = None
        self._input_stream = None
        self._output_stream = None
        self._playback_queue = queue.Queue(maxsize=50)
        self._pending_audio = bytearray()
        self._numpy = None
        self._error_reported = False
        self._error_lock = threading.Lock()

    def start(self):
        import numpy
        import sounddevice

        self._numpy = numpy
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
            raise ConnectionError("Không thể đăng ký voice UDP với Game Node.")

        self._socket.settimeout(0.5)
        self._running = True
        try:
            self._output_stream = sounddevice.OutputStream(
                samplerate=SAMPLE_RATE,
                channels=1,
                dtype="int16",
                blocksize=BLOCK_SIZE,
                callback=self._play_audio
            )
            self._input_stream = sounddevice.InputStream(
                samplerate=SAMPLE_RATE,
                channels=1,
                dtype="int16",
                blocksize=BLOCK_SIZE,
                callback=self._send_audio
            )
            self._output_stream.start()
            self._input_stream.start()
        except Exception:
            self.stop()
            raise

        self._receiver_thread = threading.Thread(target=self._receive_audio, daemon=True)
        self._receiver_thread.start()

    def _send_audio(self, indata, _frames, _time_info, _status):
        del _frames, _time_info
        udp_socket = self._socket
        if not self._running or not udp_socket:
            return
        try:
            udp_socket.sendto(
                VOICE_PACKET_PREFIX + indata.tobytes(),
                (self.host, self.port)
            )
        except OSError as exc:
            if self._running:
                self._report_error(f"Không thể gửi voice UDP: {exc}")

    def _receive_audio(self):
        udp_socket = self._socket
        while self._running and udp_socket:
            try:
                packet, _ = udp_socket.recvfrom(MAX_PACKET_BYTES + 1)
            except socket.timeout:
                continue
            except OSError as exc:
                if self._running:
                    self._report_error(f"Không thể nhận voice UDP: {exc}")
                break

            if not packet.startswith(VOICE_PACKET_PREFIX):
                continue
            audio_data = packet[len(VOICE_PACKET_PREFIX):]
            if not audio_data or len(audio_data) % 2:
                continue
            if self._playback_queue.full():
                try:
                    self._playback_queue.get_nowait()
                except queue.Empty:
                    pass
            try:
                self._playback_queue.put_nowait(audio_data)
            except queue.Full:
                pass

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
        for stream in (self._input_stream, self._output_stream):
            if stream:
                try:
                    stream.stop()
                except Exception as exc:
                    self._report_error(f"Không thể dừng thiết bị voice: {exc}")
                try:
                    stream.close()
                except Exception as exc:
                    self._report_error(f"Không thể đóng thiết bị voice: {exc}")
        self._input_stream = None
        self._output_stream = None

        if self._socket:
            self._socket.close()
            self._socket = None
        if self._receiver_thread and self._receiver_thread is not threading.current_thread():
            self._receiver_thread.join(timeout=1)
        self._receiver_thread = None
