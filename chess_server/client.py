import socket
import json
import time

def connect_to_system():
    master_host = '127.0.0.1'
    master_port = 8000
    
    # Bước 1: Gõ cửa Master Server
    print("[-] Đang kết nối tới Master Server...")
    master_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    master_socket.connect((master_host, master_port))
    
    # Xin tìm trận
    request = {"action": "find_match", "username": "Khoa"}
    master_socket.send(json.dumps(request).encode('utf-8'))
    
    # Nhận lệnh Handoff từ Master
    master_response = json.loads(master_socket.recv(1024).decode('utf-8'))
    print(f"[*] Phản hồi từ Master: {master_response}")
    master_socket.close() # Ngắt kết nối Master
    
    if master_response.get("status") == "handoff":
        node_ip = master_response["node_ip"]
        node_port = master_response["node_port"]
        token = master_response["token"]
        
        print(f"\n[-] Master yêu cầu chuyển mạng. Đang kết nối tới Game Node ({node_ip}:{node_port})...")
        time.sleep(1) # Tạo độ trễ ảo để dễ quan sát
        
        # Bước 2: Kết nối thẳng vào Game Node
        game_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        game_socket.connect((node_ip, node_port))
        
        # Gửi Token để vào phòng
        join_request = {"action": "join_game", "token": token}
        game_socket.send(json.dumps(join_request).encode('utf-8'))
        
        # Nhận xác nhận vào phòng thành công
        game_response = json.loads(game_socket.recv(1024).decode('utf-8'))
        print(f"[*] Phản hồi từ Game Node: {game_response['message']}")
        
        # Từ đây, game_socket sẽ được dùng để gửi/nhận nước đi cờ vua
        # game_socket.send(...) 

if __name__ == "__main__":
    connect_to_system()