from firebase_admin import firestore
import uuid

# Khởi tạo client kết nối đến Firestore
# (Hàm này tự động dùng chứng chỉ đã nạp bên auth_verify.py)
db = firestore.client()

def get_or_create_user_profile(uid, email):
    """
    Truy vấn hồ sơ user theo UID. 
    Nếu chưa có (user mới đăng ký), tự động tạo hồ sơ mặc định.
    """
    user_ref = db.collection('users').document(uid)
    doc = user_ref.get()
    
    if doc.exists:
        # Nếu đã có, trả về dữ liệu dạng Dictionary
        return doc.to_dict()
    else:
        # Nếu chưa có, tạo cấu trúc dữ liệu mới
        username = email.split('@')[0] # Cắt lấy phần tên làm username mặc định
        new_profile = {
            "email": email,
            "username": username,
            "rank_points": 1000,  # Cấp mặc định 1000 Elo
            "friends": []
        }
        user_ref.set(new_profile)
        print(f"[DB] Đã tạo hồ sơ Firestore mới cho UID: {uid}")
        return new_profile

def update_rank_points(uid, points_to_add):
    """Hàm dùng sau khi kết thúc trận đấu để cộng/trừ điểm"""
    user_ref = db.collection('users').document(uid)
    # Lệnh Increment giúp tránh xung đột dữ liệu khi nhiều nguồn ghi cùng lúc
    user_ref.update({
        "rank_points": firestore.Increment(points_to_add)
    })


def save_game_record_for_players(record):
    """Lưu cùng một bản ghi trận vào hồ sơ của từng người chơi."""
    player_uids = {
        player["uid"]
        for player in record["players"]
    }
    batch = db.batch()
    for uid in player_uids:
        match_ref = (
            db.collection("users")
            .document(uid)
            .collection("matches")
            .document(record["match_id"])
        )
        batch.set(match_ref, record)
    batch.commit()


def get_game_records(uid, limit=50):
    """Trả về các trận gần đây của một người chơi."""
    matches = (
        db.collection("users")
        .document(uid)
        .collection("matches")
        .order_by("completed_at", direction=firestore.Query.DESCENDING)
        .limit(limit)
        .stream()
    )
    return [
        {**match.to_dict(), "match_id": match.id}
        for match in matches
    ]


def get_game_record(uid, match_id):
    """Chỉ đọc bản ghi trận nằm trong hồ sơ của người chơi yêu cầu."""
    match = (
        db.collection("users")
        .document(uid)
        .collection("matches")
        .document(match_id)
        .get()
    )
    if not match.exists:
        return None
    return {**match.to_dict(), "match_id": match.id}


def save_lobby_chat_message(message, match_player_uids=()):
    """Lưu tin nhắn sảnh và gắn tin nhắn vào biên bản trận nếu có."""
    message_id = message.get("message_id") or uuid.uuid4().hex
    stored_message = {
        key: value
        for key, value in message.items()
        if key != "record"
    }
    stored_message["message_id"] = message_id
    batch = db.batch()
    batch.set(
        db.collection("lobby_chats").document(message_id),
        stored_message
    )
    if message.get("match_id"):
        if message.get("record"):
            batch.set(
                db.collection("shared_matches").document(message["match_id"]),
                message["record"]
            )
        for uid in set(match_player_uids):
            chat_ref = (
                db.collection("users")
                .document(uid)
                .collection("matches")
                .document(message["match_id"])
                .collection("chat")
                .document(message_id)
            )
            batch.set(chat_ref, stored_message)
    batch.commit()
    return stored_message


def get_lobby_chat_messages(limit=50):
    messages = (
        db.collection("lobby_chats")
        .order_by("created_at", direction=firestore.Query.DESCENDING)
        .limit(limit)
        .stream()
    )
    return [
        {**message.to_dict(), "message_id": message.id}
        for message in reversed(list(messages))
    ]


def get_match_chat_messages(uid, match_id, limit=100):
    match_ref = (
        db.collection("users")
        .document(uid)
        .collection("matches")
        .document(match_id)
    )
    if not match_ref.get().exists:
        return None
    messages = (
        match_ref.collection("chat")
        .order_by("created_at", direction=firestore.Query.ASCENDING)
        .limit(limit)
        .stream()
    )
    return [
        {**message.to_dict(), "message_id": message.id}
        for message in messages
    ]


def save_match_chat_message_for_players(match_id, message, player_uids):
    """Persist one in-game chat message in every participant's match history."""
    batch = db.batch()
    for uid in set(player_uids):
        chat_ref = (
            db.collection("users")
            .document(uid)
            .collection("matches")
            .document(match_id)
            .collection("chat")
            .document(message["message_id"])
        )
        batch.set(chat_ref, message)
    batch.commit()


def get_shared_game_record(match_id):
    match = (
        db.collection("shared_matches")
        .document(match_id)
        .get()
    )
    if not match.exists:
        return None
    return {**match.to_dict(), "match_id": match.id}