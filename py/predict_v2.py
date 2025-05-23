import socket
import struct
import torch
import torch.nn as nn
import onnxruntime as ort
import numpy as np
import cv2
from PIL import Image
from queue import Queue
import torchvision.transforms as T
import torchvision.models as models
import threading
from threading import Thread, Semaphore
import nms
import io
import os
import glob
import time
os.chdir("C:/Users/smsla/MultiAgent/py")
print(os.getcwd())
# 모델 로드 (한 번만 실행)
onnx_model_path = "best.onnx"
session = ort.InferenceSession(onnx_model_path)

class GraspabilityModel(nn.Module):
    def __init__(self, feature_dim=64, mid_dim=256):
        super(GraspabilityModel, self).__init__()
        mobilenet = models.mobilenet_v2(pretrained=True)
        self.features = mobilenet.features
        self.pool = nn.AdaptiveAvgPool2d((1, 1))
        self.flatten = nn.Flatten()
        self.fc1 = nn.Linear(1280, mid_dim)      # 1280 → 256
        self.fc2 = nn.Linear(mid_dim, feature_dim)  # 256 → 128
        self.fc3 = nn.Linear(feature_dim, 1)     # 128 → 1

    def forward(self, x):
        x = self.features(x)
        x = self.pool(x)
        x = self.flatten(x)
        x = torch.relu(self.fc1(x))
        feature_vec = torch.relu(self.fc2(x))    # (N, 128)
        grasp_prob = torch.sigmoid(self.fc3(feature_vec)).squeeze(1)
        return grasp_prob, feature_vec
    

# 이미지 전처리 함수
def preprocess_image(image):
    transform = T.Compose([
        T.Resize(736),
        T.ToTensor()
    ])
    img_tensor = transform(image).unsqueeze(0)
    return img_tensor.numpy()

def clean_online_data():
    save_dir = "online_data"
    if not os.path.exists(save_dir):
        return
    for fname in os.listdir(save_dir):
        if fname.endswith(".png") and not (fname.endswith("_0.png") or fname.endswith("_1.png")):
            file_path = os.path.join(save_dir, fname)
            print(f"Deleting leftover file without feedback: {file_path}")
            os.remove(file_path)

def get_next_data_no():
    save_dir = "online_data"
    pattern = os.path.join(save_dir, f"*_*_*.png")
    files = glob.glob(pattern)
    max_no = -1
    for f in files:
        base = os.path.basename(f)
        parts = base.split("_")
        if len(parts) >= 3:
            try:
                no = int(parts[0])
                if no > max_no:
                    max_no = no
            except ValueError:
                continue
    return max_no + 1

# Bounding Box 시각화 함수
def draw_bounding_boxes(image, detections, save_path="output.png"):
    img = np.array(image)
    colors = {
        0: (255, 0, 0),   # Class 0: Red
        1: (0, 255, 0),   # Class 1: Green
        2: (0, 0, 255),   # Class 2: Blue
    }
    for det in detections[0]:
        x1, y1, x2, y2, conf, class_id = det
        x1, y1, x2, y2 = map(int, [x1, y1, x2, y2])
        x = (x1+x2)//2
        y = (y1+y2)//2
        width = 120
        height = 120
        x_new1, y_new1, x_new2, y_new2 = map(int, [x-(width/2), y-(height/2), x+(width/2), y+(height/2)])
        color = colors.get(int(class_id), (0, 255, 0))
        #cv2.rectangle(img, (x1, y1), (x2, y2), color, 2)
        cv2.rectangle(img, (x_new1, y_new1), (x_new2, y_new2), color, 2)
        cv2.putText(img, f"Class {int(class_id)}: {conf:.2f}", (x1, y1 - 10),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 2)
    cv2.imwrite(save_path, cv2.cvtColor(img, cv2.COLOR_RGB2BGR))

# YOLO 추론 및 NMS 적용
def run_inference(img):
    img_array = preprocess_image(img)
    input_name = session.get_inputs()[0].name
    output_name = session.get_outputs()[0].name
    raw_output = session.run([output_name], {input_name: img_array.astype(np.float32)})
    output_data = raw_output[0].squeeze(0)
    
    conf_thres = 0.45
    iou_thres = 0.35
    mask = output_data[:, 4] > conf_thres
    boxes = output_data[mask, :4]
    confidence = output_data[mask, 4]
    class_probs = output_data[mask, 5:]
    prediction = torch.cat((torch.tensor(boxes), torch.tensor(confidence).unsqueeze(1), torch.tensor(class_probs)), 1)
    prediction = prediction.unsqueeze(0)
    
    # NMS 적용
    nms_output = nms.non_max_suppression(prediction, conf_thres=conf_thres, iou_thres=iou_thres)
    
    return nms_output

def extract_objects(image, detections):
    save_dir="cropped_objects"
    os.makedirs(save_dir, exist_ok=True)
    objects = []
    for i, det in enumerate(detections[0]):
        x1, y1, x2, y2, conf, class_id = map(int, det[:6])
        
        # 중심 좌표 계산
        x = (x1 + x2) // 2
        y = (y1 + y2) // 2
        width = 120
        height = 120

        # 크롭 영역 계산 (이미지 경계 확인)
        x_new1 = max(0, x - (width // 2))
        y_new1 = max(0, y - (height // 2))
        x_new2 = min(image.shape[1], x + (width // 2))
        y_new2 = min(image.shape[0], y + (height // 2))

        # 크롭된 이미지 추가
        cropped_img = image[y_new1:y_new2, x_new1:x_new2]
        objects.append(cropped_img)

    return objects

def run_graspability_model(instance_id):
    global data_no
    img_path = os.path.join(IMAGE_PATH, f"image_{instance_id}.png")
    img = Image.open(img_path)
    img_array = np.array(img)
    detections = run_inference(img)

    cropped_objects = extract_objects(img_array, detections)
    if cropped_objects:
        objects_tensor = torch.from_numpy(np.array(cropped_objects)).permute(0, 3, 1, 2).float().contiguous()
        with torch.no_grad():
            grasp_probs, feature_vectors = grasp_model(objects_tensor)
        
    grasp_probs_np = grasp_probs.numpy()
    best_idx = int(np.argmax(grasp_probs))
    best_feature = feature_vectors[best_idx]
    best_prob = grasp_probs_np[best_idx]
    best_img = cropped_objects[best_idx]

    # best info 출력
    x1, y1, x2, y2, conf, class_id = detections[0][best_idx]
    x = float((x1 + x2) / 2)
    y = float((y1 + y2) / 2)
    grasp_prob = grasp_probs[best_idx]
    print(f"Best Object: ({round(x, 3)}, {round(y, 3)}), Class: {class_id}, Confidence: {conf}, Graspability: {grasp_prob}")
    # response 생성
    response = struct.pack('I', len(best_feature))
    response += struct.pack(f'{len(best_feature)}f', *best_feature)
    response += struct.pack('2f', x, y)

    # img 저장
    save_dir="online_data"
    os.makedirs(save_dir, exist_ok=True)
    save_path = os.path.join(save_dir, f"{data_no}_{instance_id}_{best_prob:.4f}.png")
    data_no += 1
    cv2.imwrite(save_path, cv2.cvtColor(best_img, cv2.COLOR_RGB2BGR))

    return response

def online_learning_from_dir(batch_size=128):
    pred_dir = "online_data"
    # 피드백이 포함된 파일만 (가장 최신 128개)
    img_files = sorted(
        glob.glob(os.path.join(pred_dir, "*_[01].png")),
        key=os.path.getmtime,
        reverse=True
    )[:batch_size]
    if len(img_files) < batch_size:
        return  # 데이터가 충분하지 않으면 학습하지 않음

    images = []
    labels = []
    for img_file in img_files:
        img = cv2.imread(img_file)
        img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        img = cv2.resize(img, (120, 120))
        images.append(img)
        # 파일명에서 피드백(0/1) 추출
        feedback = int(img_file.split("_")[-1].split(".")[0])
        labels.append(feedback)

    images = np.stack(images)
    images = torch.from_numpy(images).permute(0, 3, 1, 2).float() / 255.0
    labels = torch.tensor(labels, dtype=torch.float32)

    grasp_model.train()
    optimizer = torch.optim.Adam(grasp_model.parameters(), lr=1e-4)
    criterion = torch.nn.BCELoss()
    optimizer.zero_grad()
    outputs, _ = grasp_model(images)
    loss = criterion(outputs, labels)
    loss.backward()
    optimizer.step()
    grasp_model.eval()

    # for img_file in img_files:
    #     try:
    #         os.remove(img_file)
    #     except Exception as e:
    #         print(f"Failed to delete {img_file}: {e}")

    print(f"[Online Learning] Trained on {batch_size} samples. Loss: {loss.item():.4f}")
    torch.save(grasp_model.state_dict(), "grasp_model.pth")

def log_metrics():
    global processed_requests
    avg_response_time = sum(response_times) / len(response_times) if response_times else 0
    failure_rate = (failure_count / request_count) * 100 if request_count > 0 else 0
    gpu_memory = torch.cuda.memory_allocated() / (1024 ** 2) if torch.cuda.is_available() else 0

    print(f"Processed Requests: {processed_requests}")
    print(f"Average Response Time: {avg_response_time:.2f} seconds")
    print(f"Failure Rate: {failure_rate:.2f}%")
    print(f"GPU Memory Usage: {gpu_memory:.2f} MB")

def handle_client(client_socket):
    global failure_count, request_count, processed_requests
    start_time = time.time()
    request_count += 1

    with concurrent_clients:  # Limit concurrent clients
        try:
            message = client_socket.recv(1024).decode("utf-8").strip()
            if "," in message:
                parts = message.split(",")
                if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
                    instance_id = int(parts[0])
                    success = int(parts[1])
                    print(f"Received instance_id: {instance_id}, success: {success}")
                else:
                    print(f"Invalid message: {message}")
                    client_socket.close()
                    failure_count += 1
                    return
            elif message.isdigit():
                instance_id = int(message)
                success = None
                print(f"Received instance_id: {instance_id}")
            else:
                print(f"Invalid message: {message}")
                client_socket.close()
                failure_count += 1
                return
            if success is not None:
                pred_dir = "online_data"
                # 파일명 형식: {data_no}_{instance_id}_{best_prob}.png 또는 {data_no}_{instance_id}_{best_prob}_{success}.png
                # 새롭게 바뀐 data_no를 포함한 형식에 맞춰 pattern 인식
                pattern = os.path.join(pred_dir, f"*_{instance_id}_*.png")
                candidates = sorted(glob.glob(pattern), key=os.path.getmtime, reverse=True)
                for fname in candidates:
                    base = os.path.basename(fname)
                    parts = base.replace(".png", "").split("_")
                    # 파일명 형식: {data_no}_{instance_id}_{best_prob}.png (success 없는 파일만 처리)
                    if len(parts) == 3:
                        data_no_str, inst_id_str, best_prob_str = parts
                        new_name = os.path.join(pred_dir, f"{data_no_str}_{inst_id_str}_{best_prob_str}_{success}.png")
                        os.rename(fname, new_name)
                        break

            response = run_graspability_model(instance_id)
            client_socket.sendall(response)
            processed_requests += 1

            pred_dir = "online_data"
            feedback_files = glob.glob(os.path.join(pred_dir, "*_[01].png"))
            # === 32, 64, 96, ... 개가 될 때마다 학습 ===
            if len(feedback_files) % 128 == 0:
                online_learning_from_dir(batch_size=128)

        except Exception as e:
            print(f"Client disconnected: {e}")
            failure_count += 1
        finally:
            client_socket.close()
            response_time = time.time() - start_time
            response_times.append(response_time)
            #log_metrics()


def worker():
    while True:
        client_socket = request_queue.get()
        if client_socket is None:
            break
        handle_client(client_socket)

# 서버 설정
HOST = '127.0.0.1'
PORT = 7779
IMAGE_PATH = "./images"

# GraspabilityModel 초기화
grasp_model = GraspabilityModel()
if os.path.exists("grasp_model.pth"):
    grasp_model.load_state_dict(torch.load("grasp_model.pth", map_location="cpu"))
    print("Loaded previous grasp_model weights.")
else:
    print("No previous weights found. Using random initialization.")
grasp_model.eval()

script_dir = os.path.dirname(os.path.abspath(__file__))
os.chdir(script_dir)
absolute_path = os.path.abspath(IMAGE_PATH)
print("이미지 파일의 절대 경로:", absolute_path)

clean_online_data()
global data_no
data_no = get_next_data_no()

server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
server_socket.bind((HOST, PORT))
server_socket.listen(20)
server_socket.settimeout(None)  # 무한 대기 (기본값)

request_queue = Queue()

print(f"Server listening on {HOST}:{PORT}")

# Metrics tracking
concurrent_clients = Semaphore(16)  # Limit to 4 concurrent clients (adjust as needed)
processed_requests = 0
request_count = 0
failure_count = 0
response_times = []

for _ in range(1):
    Thread(target=worker, daemon=True).start()

while True:
    client_sock, addr = server_socket.accept()
    # print(f"Connection from {addr}")
    #threading.Thread(target=handle_client, args=(client_sock,)).start()
    request_queue.put(client_sock)

