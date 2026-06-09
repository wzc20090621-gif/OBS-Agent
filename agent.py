import configparser
import os
import json
import time
import base64
import re
import threading
import queue
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox
from datetime import datetime, timedelta
from io import BytesIO
from PIL import Image, ImageDraw, ImageFont
import pyautogui
import pyperclip
from openai import OpenAI
from obsws_python import ReqClient

# ==================== 全局配置 ====================
CAPTURE_BUFFER = 0.8
pyautogui.FAILSAFE = True
DEFAULT_MAX_STEPS = 30


# ==================== 配置加载 ====================
def load_config(config_path="config.ini"):
    config = configparser.ConfigParser()
    if os.path.exists(config_path):
        config.read(config_path, encoding='utf-8')

    sections = {
        'key': ['api_key', 'base_url', 'model'],
        'obs': ['host', 'port', 'password', 'scene']
    }

    result = {}
    for section, keys in sections.items():
        if not config.has_section(section):
            config.add_section(section)
        for key in keys:
            if not config.has_option(section, key):
                config.set(section, key, '')
        result[section] = {k: config.get(section, k) for k in keys}

    return result, config, config_path


def save_config(config, config_path="config.ini"):
    with open(config_path, 'w', encoding='utf-8') as f:
        config.write(f)


# ==================== 截图 ====================
def capture_screen(obs_client, source_name):
    screen_w, screen_h = pyautogui.size()
    request_data = {
        "sourceName": source_name,
        "imageFormat": "png",
        "imageWidth": screen_w,
        "imageHeight": screen_h
    }
    resp = obs_client.send("GetSourceScreenshot", request_data)
    if isinstance(resp, dict):
        img_b64 = resp.get("imageData", "")
    else:
        img_b64 = getattr(resp, "imageData", "") or getattr(resp, "image_data", "")
    if not img_b64:
        raise RuntimeError("OBS 返回的截图 imageData 为空")
    if isinstance(img_b64, str) and img_b64.startswith("data:"):
        img_b64 = img_b64.split(",", 1)[1]
    return img_b64


# ==================== 三层边缘网格（老版本，不挡画面） ====================
def add_grid_to_image(img_b64, screen_w, screen_h):
    """
    边缘三层刻度，不铺满全屏：
    - 200px: 粗线 + 大字
    - 100px: 中线 + 小字
    - 50px:  短线无字
    """
    b64 = re.sub(r'^data:image/\w+;base64,', '', img_b64)
    img_data = base64.b64decode(b64)
    img = Image.open(BytesIO(img_data)).convert("RGBA")

    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    try:
        font_big   = ImageFont.truetype("consola.ttf", 12)
        font_small = ImageFont.truetype("consola.ttf", 9)
    except:
        font_big = font_small = ImageFont.load_default()

    for x in range(0, screen_w, 50):
        if x % 200 == 0:
            draw.line([(x, 0), (x, 22)], fill=(255, 50, 50, 160), width=1)
            draw.text((x + 3, 24), str(x), fill=(255, 60, 60, 200), font=font_big)
        elif x % 100 == 0:
            draw.line([(x, 0), (x, 14)], fill=(255, 50, 50, 100), width=1)
            draw.text((x + 3, 15), str(x), fill=(255, 60, 60, 130), font=font_small)
        else:
            draw.line([(x, 0), (x, 6)], fill=(255, 50, 50, 60), width=1)

    for y in range(0, screen_h, 50):
        if y % 200 == 0:
            draw.line([(0, y), (22, y)], fill=(255, 50, 50, 160), width=1)
            draw.text((24, y - 7), str(y), fill=(255, 60, 60, 200), font=font_big)
        elif y % 100 == 0:
            draw.line([(0, y), (14, y)], fill=(255, 50, 50, 100), width=1)
            draw.text((16, y - 6), str(y), fill=(255, 60, 60, 130), font=font_small)
        else:
            draw.line([(0, y), (6, y)], fill=(255, 50, 50, 60), width=1)

    draw.text((screen_w - 155, screen_h - 20), f"{screen_w} x {screen_h}",
              fill=(255, 60, 60, 180), font=font_big)

    img = Image.alpha_composite(img, overlay).convert("RGB")
    buf = BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


# ==================== 鼠标高亮 ====================
def add_cursor_highlight(img_b64, screen_w, screen_h):
    b64 = re.sub(r'^data:image/\w+;base64,', '', img_b64)
    img_data = base64.b64decode(b64)
    img = Image.open(BytesIO(img_data)).convert("RGBA")

    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    mx, my = pyautogui.position()

    try:
        font = ImageFont.truetype("consola.ttf", 11)
    except:
        font = ImageFont.load_default()

    draw.line([(mx - 25, my), (mx + 25, my)], fill=(0, 255, 80, 200), width=2)
    draw.line([(mx, my - 25), (mx, my + 25)], fill=(0, 255, 80, 200), width=2)

    label_x = mx + 30 if mx < screen_w - 100 else mx - 100
    label_y = my - 18 if my > 40 else my + 30
    draw.text((label_x, label_y), f"({mx},{my})", fill=(0, 255, 80, 230), font=font)

    img = Image.alpha_composite(img, overlay).convert("RGB")
    buf = BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


# ==================== 系统提示词（精简版） ====================
def build_system_prompt(screen_w, screen_h):
    return f"""你是电脑操控智能体。根据截图和操作历史决定下一步。

屏幕: {screen_w}x{screen_h}px。
截图顶边和左边有三层红色刻度：粗线+大字=200px | 中线+小字=100px | 短线=50px。
绿色十字线标记鼠标当前位置，附坐标数字。

## 定位
找目标最近刻度数字，数短线估算精确像素。例：目标在 300 和 400 之间看 350 处短线 → X≈350。

## 动作
- aim_click{{x,y}}: 先移到(x,y)再单击（**首选**）
- aim_double{{x,y}}: 先移到(x,y)再双击（桌面图标/文件首选）
- click/double_click{{x,y}}: 原地点击（仅鼠标已对准时）
- move{{x,y}} / type{{text}} / key{{keys:["win"]}} / wait{{duration}} / done{{}}

## 策略
- 同样操作 2 次没变化换方法。快捷键优先：Win→搜索→typing→Enter
- 快捷键: Ctrl+N=新建, Ctrl+S=保存, Esc=关闭, Alt+F4=关窗口

## JSON
{{"thinking":"一句话","action":{{"type":"...",...}}}}"""


# ==================== 动作执行器 ====================
def execute_action(action: dict):
    action_type = action.get("type", "")
    screen_w, screen_h = pyautogui.size()

    def _fix_xy(x, y):
        if 0 < x <= 1 and 0 < y <= 1 and screen_w > 1:
            x = int(x * screen_w)
            y = int(y * screen_h)
        return max(0, min(int(x), screen_w - 1)), max(0, min(int(y), screen_h - 1))

    if action_type == "move":
        x, y = _fix_xy(action["x"], action["y"])
        pyautogui.moveTo(x, y, duration=0.1)
        return f"➡️ 移动 ({x}, {y})"

    elif action_type == "aim_click":
        x, y = _fix_xy(action["x"], action["y"])
        pyautogui.moveTo(x, y, duration=0.1)
        time.sleep(0.05)
        pyautogui.click(x, y)
        return f"🎯 瞄准点击 ({x}, {y})"

    elif action_type == "aim_double":
        x, y = _fix_xy(action["x"], action["y"])
        pyautogui.moveTo(x, y, duration=0.1)
        time.sleep(0.05)
        pyautogui.doubleClick(x, y)
        return f"🎯 瞄准双击 ({x}, {y})"

    elif action_type == "click":
        x, y = _fix_xy(action["x"], action["y"])
        pyautogui.click(x, y)
        return f"🖱️ 单击 ({x}, {y})"

    elif action_type == "double_click":
        x, y = _fix_xy(action["x"], action["y"])
        pyautogui.doubleClick(x, y)
        return f"🖱️ 双击 ({x}, {y})"

    elif action_type == "right_click":
        x, y = _fix_xy(action["x"], action["y"])
        pyautogui.rightClick(x, y)
        return f"🖱️ 右键 ({x}, {y})"

    elif action_type == "drag":
        x, y = _fix_xy(action["x"], action["y"])
        x2, y2 = _fix_xy(action["x2"], action["y2"])
        pyautogui.moveTo(x, y)
        pyautogui.drag(x2 - x, y2 - y, duration=0.3)
        return f"↔️ 拖拽 ({x},{y})→({x2},{y2})"

    elif action_type == "type":
        text = action["text"]
        pyperclip.copy(text)
        pyautogui.hotkey("ctrl", "v")
        return f"⌨️ 输入: {text}"

    elif action_type == "key":
        keys = action["keys"]
        pyautogui.hotkey(*keys)
        return f"⌨️ 按键: {'+'.join(keys)}"

    elif action_type == "scroll":
        amount = action.get("amount", 0)
        pyautogui.scroll(amount)
        return f"🖱️ 滚轮: {amount}"

    elif action_type == "wait":
        duration = max(1, min(float(action.get("duration", 1)), 5))
        time.sleep(duration)
        return f"⏳ 等待 {duration}s"

    elif action_type == "done":
        return "✅ 任务完成"

    return f"⚠️ 未知动作: {action_type}"


# ==================== 智能体 ====================
class Agent:
    def __init__(self, ai_client, obs_client, ai_model, obs_scene, task, log_cb=None):
        self.ai = ai_client
        self.obs = obs_client
        self.model = ai_model
        self.scene = obs_scene
        self.task = task
        self.screen_w, self.screen_h = pyautogui.size()
        self.action_log = []
        self.system_prompt = build_system_prompt(self.screen_w, self.screen_h)
        self.log = log_cb or print
        self._stop_flag = False

    def stop(self):
        self._stop_flag = True

    def run(self, max_steps=30):
        self._stop_flag = False
        self.log(f"🤖 启动 | 📺 {self.screen_w}x{self.screen_h} | 📋 {self.task} | 🔄 {max_steps}步")

        for step in range(1, max_steps + 1):
            if self._stop_flag:
                self.log("⏹️ 手动停止")
                return False

            self.log(f"--- Step {step}/{max_steps} ---")
            time.sleep(CAPTURE_BUFFER)

            try:
                img_b64 = capture_screen(self.obs, self.scene)
            except Exception as e:
                self.log(f"❌ 截图失败: {e}")
                return False

            messages = self._build_messages(img_b64)

            try:
                response_text = self.ai.chat.completions.create(
                    model=self.model,
                    messages=messages
                ).choices[0].message.content
            except Exception as e:
                self.log(f"❌ AI 调用失败: {e}")
                return False

            action = self._parse_response(response_text)
            if action is None:
                self.log("⚠️ 格式错误，跳过")
                continue

            thinking = action.get("thinking", "...")
            act = action.get("action", {})
            self.log(f"💭 {thinking}")

            if act.get("type") == "done":
                self.log(f"🎉 完成！{step}步")
                return True

            try:
                desc = execute_action(act)
                self.log(desc)
            except Exception as e:
                self.log(f"❌ 执行失败: {e}")

            self.action_log.append(f"S{step}: {thinking[:50]} → {act.get('type')}")

        self.log(f"⚠️ 达到上限 {max_steps}步")
        return False

    def _build_messages(self, img_b64):
        img_b64 = add_grid_to_image(img_b64, self.screen_w, self.screen_h)
        img_b64 = add_cursor_highlight(img_b64, self.screen_w, self.screen_h)

        history_block = "历史:\n" + "\n".join(self.action_log[-3:]) if self.action_log else "第一步"

        return [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": [
                {"type": "text", "text": f"任务: {self.task}\n{history_block}"},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{img_b64}"}}
            ]}
        ]

    def _parse_response(self, text):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        fixed = re.sub(r'"type":"(\w+)":', r'"type":"type","\1":', text)
        fixed = re.sub(r'"type","text":', '"type":"type","text":', fixed)
        if fixed != text:
            try:
                return json.loads(fixed)
            except json.JSONDecodeError:
                pass
        match = re.search(r'```(?:json)?\s*([\s\S]*?)\s*```', text)
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                pass
        match = re.search(r'\{[\s\S]*\}', text)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                pass
        self.log(f"❌ JSON:\n{text[:150]}")
        return None


# ==================== 定时任务 ====================
class TaskScheduler:
    def __init__(self, task_file="tasks.json"):
        self.task_file = task_file
        self.tasks = []
        self.load()

    def load(self):
        if os.path.exists(self.task_file):
            try:
                with open(self.task_file, 'r', encoding='utf-8') as f:
                    self.tasks = json.load(f)
            except:
                self.tasks = []

    def save(self):
        with open(self.task_file, 'w', encoding='utf-8') as f:
            json.dump(self.tasks, f, ensure_ascii=False, indent=2)

    def add(self, task_text, max_steps, schedule_type, schedule_value):
        self.tasks.append({"task": task_text, "max_steps": max_steps,
                           "schedule_type": schedule_type, "schedule_value": schedule_value,
                           "enabled": True, "last_run": None})
        self.save()

    def remove(self, index):
        if 0 <= index < len(self.tasks):
            del self.tasks[index]
            self.save()

    def toggle(self, index):
        if 0 <= index < len(self.tasks):
            self.tasks[index]["enabled"] = not self.tasks[index]["enabled"]
            self.save()

    def get_due_tasks(self):
        now = datetime.now()
        due = []
        for i, t in enumerate(self.tasks):
            if not t["enabled"]:
                continue
            rt = self._get_next_run(t)
            if rt and rt <= now:
                if t.get("last_run") and datetime.fromisoformat(t["last_run"]) > now - timedelta(minutes=1):
                    continue
                due.append((i, t))
        return due

    def mark_run(self, index):
        self.tasks[index]["last_run"] = datetime.now().isoformat()
        self.save()

    def _get_next_run(self, task):
        try:
            if task["schedule_type"] == "daily":
                t = datetime.strptime(task["schedule_value"], "%H:%M").time()
                return datetime.combine(datetime.now().date(), t)
            elif task["schedule_type"] == "hourly":
                m = int(task["schedule_value"])
                now = datetime.now()
                dt = now.replace(minute=m, second=0, microsecond=0)
                return dt if dt > now else dt + timedelta(hours=1)
        except:
            return None


# ==================== GUI ====================
class App:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("OBS-Agent")
        self.root.geometry("1000x700")
        self.log_queue = queue.Queue()
        self.agent_thread = None
        self.running = False
        self.scheduler_running = False
        self.cfg_dict, self.config_obj, self.config_path = load_config()
        self.scheduler = TaskScheduler()
        self._build_ui()
        self._start_log_poller()
        self._start_scheduler_poller()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self):
        paned = ttk.PanedWindow(self.root, orient=tk.HORIZONTAL)
        paned.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        left = ttk.Frame(paned)
        paned.add(left, weight=1)
        right = ttk.Frame(paned)
        paned.add(right, weight=2)
        self._build_left(left)
        self._build_right(right)
        bottom = ttk.Frame(self.root)
        bottom.pack(fill=tk.X, padx=5, pady=(0, 5))
        self.btn_run = ttk.Button(bottom, text="▶ 执行", command=self._run_now)
        self.btn_run.pack(side=tk.LEFT, padx=2)
        self.btn_stop = ttk.Button(bottom, text="⏹ 停止", command=self._stop, state=tk.DISABLED)
        self.btn_stop.pack(side=tk.LEFT, padx=2)
        self.status_label = ttk.Label(bottom, text="⚪ 空闲", foreground="gray")
        self.status_label.pack(side=tk.RIGHT, padx=10)

    def _build_left(self, p):
        ttk.Label(p, text="📋 任务", font=("", 10, "bold")).pack(anchor=tk.W)
        self.task_entry = ttk.Entry(p)
        self.task_entry.pack(fill=tk.X, pady=(0, 5))
        ttk.Label(p, text="🔄 最大步数").pack(anchor=tk.W)
        self.steps_var = tk.StringVar(value=str(DEFAULT_MAX_STEPS))
        ttk.Entry(p, textvariable=self.steps_var, width=10).pack(anchor=tk.W, pady=(0, 5))
        ttk.Label(p, text="⏰ 定时", font=("", 10, "bold")).pack(anchor=tk.W, pady=(10, 2))
        self.sched_type = tk.StringVar(value="none")
        ttk.Radiobutton(p, text="不启用", variable=self.sched_type, value="none").pack(anchor=tk.W)
        ttk.Radiobutton(p, text="每天", variable=self.sched_type, value="daily").pack(anchor=tk.W)
        ttk.Radiobutton(p, text="每小时", variable=self.sched_type, value="hourly").pack(anchor=tk.W)
        tf = ttk.Frame(p)
        tf.pack(fill=tk.X, pady=(2, 5))
        ttk.Label(tf, text="时间:").pack(side=tk.LEFT)
        self.sched_time = ttk.Entry(tf, width=8)
        self.sched_time.pack(side=tk.LEFT, padx=2)
        ttk.Label(tf, text="(HH:MM)").pack(side=tk.LEFT)
        ttk.Button(p, text="➕ 添加定时", command=self._add_scheduled).pack(fill=tk.X, pady=(0, 10))
        ttk.Label(p, text="📅 定时列表", font=("", 10, "bold")).pack(anchor=tk.W)
        lf = ttk.Frame(p)
        lf.pack(fill=tk.BOTH, expand=True)
        self.task_list = ttk.Treeview(lf, columns=("t", "s", "st"), show="headings", height=6)
        self.task_list.heading("t", text="任务")
        self.task_list.heading("s", text="定时")
        self.task_list.heading("st", text="状态")
        self.task_list.column("t", width=180)
        self.task_list.column("s", width=100)
        self.task_list.column("st", width=50)
        self.task_list.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb = ttk.Scrollbar(lf, orient=tk.VERTICAL, command=self.task_list.yview)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.task_list.configure(yscrollcommand=sb.set)
        bf = ttk.Frame(p)
        bf.pack(fill=tk.X, pady=2)
        ttk.Button(bf, text="启用/禁用", command=self._toggle_sched).pack(side=tk.LEFT, padx=2)
        ttk.Button(bf, text="删除", command=self._remove_sched).pack(side=tk.LEFT, padx=2)
        self._refresh_list()
        ttk.Label(p, text="", font=("", 6)).pack()
        ttk.Button(p, text="⚙ 配置", command=self._open_cfg).pack(fill=tk.X)

    def _build_right(self, p):
        ttk.Label(p, text="📝 日志", font=("", 10, "bold")).pack(anchor=tk.W)
        self.log_area = scrolledtext.ScrolledText(p, wrap=tk.WORD, font=("Consolas", 9),
                                                   bg="#1e1e1e", fg="#d4d4d4")
        self.log_area.pack(fill=tk.BOTH, expand=True)
        self.log_area.configure(state=tk.DISABLED)

    def log(self, text):
        self.log_queue.put(text)

    def _start_log_poller(self):
        def poll():
            while not self.log_queue.empty():
                try:
                    msg = self.log_queue.get_nowait()
                    self.log_area.configure(state=tk.NORMAL)
                    self.log_area.insert(tk.END, msg + "\n")
                    self.log_area.see(tk.END)
                    self.log_area.configure(state=tk.DISABLED)
                except:
                    pass
            self.root.after(100, poll)
        self.root.after(100, poll)

    def _start_scheduler_poller(self):
        self.scheduler_running = True
        def poll():
            if not self.scheduler_running:
                return
            if not self.running:
                for idx, task in self.scheduler.get_due_tasks():
                    self._execute_task(task["task"], task["max_steps"])
                    self.scheduler.mark_run(idx)
                    self._refresh_list()
            self.root.after(5000, poll)
        self.root.after(5000, poll)

    def _run_now(self):
        task = self.task_entry.get().strip()
        if not task:
            messagebox.showwarning("提示", "请输入任务")
            return
        steps = int(self.steps_var.get() or DEFAULT_MAX_STEPS)
        self._execute_task(task, steps)

    def _execute_task(self, task, steps):
        if self.running:
            messagebox.showwarning("提示", "已有任务在运行中")
            return
        self.running = True
        self.btn_run.configure(state=tk.DISABLED)
        self.btn_stop.configure(state=tk.NORMAL)
        self.status_label.configure(text="🟢 运行中", foreground="green")
        self.log("=" * 50)
        self.log(f"🚀 {task}")
        def run():
            try:
                obs = ReqClient(host=self.cfg_dict['obs']['host'],
                                port=int(self.cfg_dict['obs']['port'] or 4455),
                                password=self.cfg_dict['obs']['password'])
                ai_client = OpenAI(api_key=self.cfg_dict['key']['api_key'],
                                   base_url=self.cfg_dict['key']['base_url'])
                agent = Agent(ai_client, obs, self.cfg_dict['key']['model'],
                             self.cfg_dict['obs']['scene'], task, log_cb=self.log)
                agent.run(max_steps=steps)
                obs.disconnect()
            except Exception as e:
                self.log(f"❌ {e}")
            finally:
                self.running = False
                self.root.after(0, self._on_done)
        self.agent_thread = threading.Thread(target=run, daemon=True)
        self.agent_thread.start()

    def _on_done(self):
        self.btn_run.configure(state=tk.NORMAL)
        self.btn_stop.configure(state=tk.DISABLED)
        self.status_label.configure(text="⚪ 空闲", foreground="gray")

    def _stop(self):
        self.running = False
        pyautogui.FAILSAFE = False

    def _add_scheduled(self):
        task = self.task_entry.get().strip()
        if not task:
            messagebox.showwarning("提示", "请输入任务")
            return
        sched = self.sched_type.get()
        if sched == "none":
            messagebox.showwarning("提示", "请选择定时方式")
            return
        val = self.sched_time.get().strip()
        if not val:
            messagebox.showwarning("提示", "请输入时间")
            return
        self.scheduler.add(task, int(self.steps_var.get() or DEFAULT_MAX_STEPS), sched, val)
        self._refresh_list()

    def _toggle_sched(self):
        sel = self.task_list.selection()
        if sel:
            self.scheduler.toggle(self.task_list.index(sel[0]))
            self._refresh_list()

    def _remove_sched(self):
        sel = self.task_list.selection()
        if sel:
            self.scheduler.remove(self.task_list.index(sel[0]))
            self._refresh_list()

    def _refresh_list(self):
        for item in self.task_list.get_children():
            self.task_list.delete(item)
        for i, t in enumerate(self.scheduler.tasks):
            st = "✅" if t["enabled"] else "❌"
            self.task_list.insert("", tk.END, iid=str(i),
                                  values=(t["task"], f"{t['schedule_type']}:{t['schedule_value']}", st))

    def _open_cfg(self):
        w = tk.Toplevel(self.root)
        w.title("配置")
        w.geometry("500x350")
        nb = ttk.Notebook(w)
        nb.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        for section in ['key', 'obs']:
            f = ttk.Frame(nb)
            nb.add(f, text=section.upper())
            entries = {}
            for i, key in enumerate(self.cfg_dict[section]):
                ttk.Label(f, text=key).grid(row=i, column=0, sticky=tk.W, padx=5, pady=3)
                var = tk.StringVar(value=self.cfg_dict[section][key])
                ttk.Entry(f, textvariable=var, width=50).grid(row=i, column=1, padx=5, pady=3)
                entries[key] = var
            def save_s(sec=section, ent=entries):
                for k, v in ent.items():
                    self.config_obj.set(sec, k, v.get())
                    self.cfg_dict[sec][k] = v.get()
                save_config(self.config_obj, self.config_path)
                messagebox.showinfo("提示", f"[{sec}] 已保存")
            ttk.Button(f, text="保存", command=save_s).grid(
                row=len(self.cfg_dict[section]), column=0, columnspan=2, pady=10)

    def _on_close(self):
        self.running = False
        self.scheduler_running = False
        pyautogui.FAILSAFE = False
        self.root.destroy()

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    App().run()
