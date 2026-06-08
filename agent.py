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
CAPTURE_BUFFER = 0.3
pyautogui.FAILSAFE = True
DEFAULT_MAX_STEPS = 30


# ==================== 配置加载（复用现有逻辑） ====================
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


# ==================== 截图 & 网格 ====================
def capture_screen(obs_client, source_name):
    request_data = {"sourceName": source_name, "imageFormat": "png"}
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


def add_grid_to_image(img_b64, screen_w, screen_h):
    """
    两层刻度：
    - 100px: 实线 + 数字（醒目）
    - 50px:  短线，无数字，约30%透明度（辅助估算）
    """
    b64 = re.sub(r'^data:image/\w+;base64,', '', img_b64)
    img_data = base64.b64decode(b64)
    img = Image.open(BytesIO(img_data)).convert("RGBA")

    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    try:
        font = ImageFont.truetype("consola.ttf", 12)
    except:
        font = ImageFont.load_default()

    # ── X 轴（顶边） ──
    for x in range(0, screen_w, 50):
        if x % 100 == 0:
            draw.line([(x, 0), (x, 18)], fill=(255, 50, 50, 180), width=1)
            draw.text((x + 3, 20), str(x), fill=(255, 60, 60, 220), font=font)
        else:
            draw.line([(x, 0), (x, 8)], fill=(255, 50, 50, 55), width=1)

    # ── Y 轴（左边） ──
    for y in range(0, screen_h, 50):
        if y % 100 == 0:
            draw.line([(0, y), (18, y)], fill=(255, 50, 50, 180), width=1)
            draw.text((20, y - 7), str(y), fill=(255, 60, 60, 220), font=font)
        else:
            draw.line([(0, y), (8, y)], fill=(255, 50, 50, 55), width=1)

    # ── 右下角分辨率 ──
    draw.text((screen_w - 140, screen_h - 18), f"{screen_w}x{screen_h}",
              fill=(255, 60, 60, 200), font=font)

    img = Image.alpha_composite(img, overlay).convert("RGB")
    buf = BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()

# ==================== 系统提示词 ====================
def build_system_prompt(screen_w, screen_h):
    return f"""你是电脑操控智能体。根据截图和操作历史决定下一步。

屏幕: {screen_w}x{screen_h}px。
截图顶边和左边有红色坐标刻度：
实线+数字=100px | 虚线(半透明)=50px。

## ⚠️ 坐标方向（必须牢记，绝对不可搞反）
- (0,0) 在左上角
- X 轴向右增长：左边小、右边大。屏幕左侧 X≈0，右侧 X≈{screen_w}
- Y 轴向下增长：上边小、下边大。屏幕顶部 Y≈0，底部 Y≈{screen_h}
- 说"左" = X 值更小，"右" = X 值更大
- 说"上" = Y 值更小，"下" = Y 值更大
- 如果上一步鼠标高度对了但左右反了，说明你把左右搞混了。用刻度线确认 X 值，而不是凭感觉

## 操作策略
1. 用刻度线读取目标精确坐标，直接 move 过去。
2. 打开菜单/搜索后别点外面，直接 typing
3. 在桌面打开应用或文件时记得双击或右键打开更多选项
4. 每次操作完观察鼠标位置进行移动或微调后再点击
5. 寻找应用时可以通过win+d键回到桌面
6. 如多次操作出错则更换方法

## 输出纯 JSON
{{"thinking":"目标在哪、刻度读数（如 X≈350 Y≈520）、方向确认（左侧X小右侧X大）","action":{{"type":"move|click|double_click|right_click|drag|type|key|scroll|wait|done",...}}}}

动作: move/click/double_click/right_click{{x,y}} | drag{{x,y,x2,y2}} | type{{text}} | key{{keys:["win"]}} | scroll{{amount}} | wait{{duration}} | done{{}}"""


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
        duration = action.get("duration", 1)
        time.sleep(duration)
        return f"⏳ 等待 {duration}s"
    elif action_type == "done":
        return "✅ 任务完成"
    return f"⚠️ 未知动作: {action_type}"


# ==================== 智能体（可回调版本） ====================
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
        self.log(f"🤖 智能体启动 | 📺 {self.screen_w}x{self.screen_h} | 📋 {self.task} | 🔄 {max_steps}步")

        for step in range(1, max_steps + 1):
            if self._stop_flag:
                self.log("⏹️ 用户手动停止")
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
                self.log("⚠️ AI 返回格式错误，跳过")
                continue

            thinking = action.get("thinking", "...")
            act = action.get("action", {})
            self.log(f"💭 {thinking}")

            if act.get("type") == "done":
                self.log(f"🎉 完成！共 {step} 步")
                return True

            try:
                desc = execute_action(act)
                self.log(desc)
            except Exception as e:
                self.log(f"❌ 执行失败: {e}")

            self.action_log.append(f"Step {step}: {thinking} → {act.get('type')}")

        self.log(f"⚠️ 达到最大步数 {max_steps}")
        return False

    def _build_messages(self, img_b64):
        img_b64 = add_grid_to_image(img_b64, self.screen_w, self.screen_h)
        if self.action_log:
            history_block = "最近操作:\n" + "\n".join(self.action_log[-5:])
        else:
            history_block = "第一步"
        return [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": [
                {"type": "text", "text": f"任务: {self.task}\n\n{history_block}\n\n截图有三层红色刻度（顶边X左边Y，粗线=200/中线=100/短线=50）。决定下一步。"},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{img_b64}"}}
            ]}
        ]

    def _parse_response(self, text):
        try:
            return json.loads(text)
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
        self.log(f"❌ JSON 解析失败:\n{text[:200]}")
        return None


# ==================== 定时任务管理器 ====================
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
        """schedule_type: 'once'|'daily'|'hourly', schedule_value: 'YYYY-MM-DD HH:MM' or 'HH:MM'"""
        self.tasks.append({
            "task": task_text,
            "max_steps": max_steps,
            "schedule_type": schedule_type,
            "schedule_value": schedule_value,
            "enabled": True,
            "last_run": None
        })
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
            run_time = self._get_next_run(t)
            if run_time and run_time <= now:
                # 检查是否已经在这一分钟内跑过
                if t.get("last_run") and datetime.fromisoformat(t["last_run"]) > now - timedelta(minutes=1):
                    continue
                due.append((i, t))
        return due

    def mark_run(self, index):
        self.tasks[index]["last_run"] = datetime.now().isoformat()
        self.save()

    def _get_next_run(self, task):
        try:
            if task["schedule_type"] == "once":
                dt = datetime.strptime(task["schedule_value"], "%Y-%m-%d %H:%M")
                return dt if dt > datetime.now() - timedelta(minutes=1) else None
            elif task["schedule_type"] == "daily":
                t = datetime.strptime(task["schedule_value"], "%H:%M").time()
                dt = datetime.combine(datetime.now().date(), t)
                return dt
            elif task["schedule_type"] == "hourly":
                minute = int(task["schedule_value"])
                now = datetime.now()
                dt = now.replace(minute=minute, second=0, microsecond=0)
                if dt <= now:
                    dt += timedelta(hours=1)
                return dt
        except:
            return None
        return None


# ==================== GUI ====================
class App:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("OBS-Agent 智能体")
        self.root.geometry("1000x700")

        # 数据
        self.log_queue = queue.Queue()
        self.agent_thread = None
        self.scheduler_thread = None
        self.running = False
        self.scheduler_running = False

        # 加载配置
        self.cfg_dict, self.config_obj, self.config_path = load_config()

        # 加载定时任务
        self.scheduler = TaskScheduler()

        # 构建 GUI
        self._build_ui()
        self._start_log_poller()
        self._start_scheduler_poller()

        # 关闭处理
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ── GUI 构建 ──
    def _build_ui(self):
        # 主分栏
        paned = ttk.PanedWindow(self.root, orient=tk.HORIZONTAL)
        paned.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        # 左侧：任务管理
        left = ttk.Frame(paned)
        paned.add(left, weight=1)

        # 右侧：日志
        right = ttk.Frame(paned)
        paned.add(right, weight=2)

        self._build_left_panel(left)
        self._build_right_panel(right)

        # 底部控制栏
        bottom = ttk.Frame(self.root)
        bottom.pack(fill=tk.X, padx=5, pady=(0, 5))

        self.btn_run = ttk.Button(bottom, text="▶ 立即执行", command=self._run_now)
        self.btn_run.pack(side=tk.LEFT, padx=2)

        self.btn_stop = ttk.Button(bottom, text="⏹ 停止", command=self._stop, state=tk.DISABLED)
        self.btn_stop.pack(side=tk.LEFT, padx=2)

        self.status_label = ttk.Label(bottom, text="⚪ 空闲", foreground="gray")
        self.status_label.pack(side=tk.RIGHT, padx=10)

    def _build_left_panel(self, parent):
        # ── 当前任务 ──
        ttk.Label(parent, text="📋 任务", font=("", 10, "bold")).pack(anchor=tk.W, pady=(0, 2))
        self.task_entry = ttk.Entry(parent)
        self.task_entry.pack(fill=tk.X, pady=(0, 5))

        # ── 步数 ──
        ttk.Label(parent, text="🔄 最大步数").pack(anchor=tk.W)
        self.steps_var = tk.StringVar(value=str(DEFAULT_MAX_STEPS))
        ttk.Entry(parent, textvariable=self.steps_var, width=10).pack(anchor=tk.W, pady=(0, 5))

        # ── 定时设置 ──
        ttk.Label(parent, text="⏰ 定时执行（可选）", font=("", 10, "bold")).pack(anchor=tk.W, pady=(10, 2))

        self.sched_type = tk.StringVar(value="none")
        ttk.Radiobutton(parent, text="不启用", variable=self.sched_type, value="none").pack(anchor=tk.W)
        ttk.Radiobutton(parent, text="每天", variable=self.sched_type, value="daily").pack(anchor=tk.W)
        ttk.Radiobutton(parent, text="每小时", variable=self.sched_type, value="hourly").pack(anchor=tk.W)

        time_frame = ttk.Frame(parent)
        time_frame.pack(fill=tk.X, pady=(2, 5))
        ttk.Label(time_frame, text="时间:").pack(side=tk.LEFT)
        self.sched_time = ttk.Entry(time_frame, width=8)
        self.sched_time.pack(side=tk.LEFT, padx=2)
        ttk.Label(time_frame, text="(HH:MM 或 分钟数)").pack(side=tk.LEFT)

        ttk.Button(parent, text="➕ 添加到定时列表", command=self._add_scheduled).pack(fill=tk.X, pady=(0, 10))

        # ── 定时任务列表 ──
        ttk.Label(parent, text="📅 定时任务列表", font=("", 10, "bold")).pack(anchor=tk.W)

        list_frame = ttk.Frame(parent)
        list_frame.pack(fill=tk.BOTH, expand=True)

        self.task_list = ttk.Treeview(list_frame, columns=("task", "schedule", "status"), show="headings", height=6)
        self.task_list.heading("task", text="任务")
        self.task_list.heading("schedule", text="定时")
        self.task_list.heading("status", text="状态")
        self.task_list.column("task", width=180)
        self.task_list.column("schedule", width=100)
        self.task_list.column("status", width=60)
        self.task_list.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        scroll = ttk.Scrollbar(list_frame, orient=tk.VERTICAL, command=self.task_list.yview)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.task_list.configure(yscrollcommand=scroll.set)

        btn_frame = ttk.Frame(parent)
        btn_frame.pack(fill=tk.X, pady=2)
        ttk.Button(btn_frame, text="启用/禁用", command=self._toggle_scheduled).pack(side=tk.LEFT, padx=2)
        ttk.Button(btn_frame, text="删除", command=self._remove_scheduled).pack(side=tk.LEFT, padx=2)

        self._refresh_task_list()

        # ── 配置按钮 ──
        ttk.Label(parent, text="", font=("", 6)).pack()  # spacer
        ttk.Button(parent, text="⚙ 配置 AI / OBS", command=self._open_config).pack(fill=tk.X)

    def _build_right_panel(self, parent):
        ttk.Label(parent, text="📝 运行日志", font=("", 10, "bold")).pack(anchor=tk.W, pady=(0, 2))
        self.log_area = scrolledtext.ScrolledText(
            parent, wrap=tk.WORD, font=("Consolas", 9),
            bg="#1e1e1e", fg="#d4d4d4", insertbackground="white"
        )
        self.log_area.pack(fill=tk.BOTH, expand=True)
        self.log_area.configure(state=tk.DISABLED)

    # ── 日志处理 ──
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

    # ── 调度轮询 ──
    def _start_scheduler_poller(self):
        self.scheduler_running = True

        def poll():
            if not self.scheduler_running:
                return
            if not self.running:
                due = self.scheduler.get_due_tasks()
                for idx, task in due:
                    self._execute_task(task["task"], task["max_steps"])
                    self.scheduler.mark_run(idx)
                    self._refresh_task_list()
            self.root.after(5000, poll)  # 每5秒检查一次

        self.root.after(5000, poll)

    # ── 执行 ──
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
        self.log(f"🚀 开始执行: {task}")

        def run():
            try:
                obs = ReqClient(
                    host=self.cfg_dict['obs']['host'],
                    port=int(self.cfg_dict['obs']['port'] or 4455),
                    password=self.cfg_dict['obs']['password']
                )
                ai_client = OpenAI(
                    api_key=self.cfg_dict['key']['api_key'],
                    base_url=self.cfg_dict['key']['base_url']
                )
                agent = Agent(
                    ai_client, obs,
                    self.cfg_dict['key']['model'],
                    self.cfg_dict['obs']['scene'],
                    task,
                    log_cb=self.log
                )
                agent.run(max_steps=steps)
                obs.disconnect()
            except Exception as e:
                self.log(f"❌ 运行异常: {e}")
            finally:
                self.running = False
                self.root.after(0, self._on_task_done)

        self.agent_thread = threading.Thread(target=run, daemon=True)
        self.agent_thread.start()

    def _on_task_done(self):
        self.btn_run.configure(state=tk.NORMAL)
        self.btn_stop.configure(state=tk.DISABLED)
        self.status_label.configure(text="⚪ 空闲", foreground="gray")
        self.log("=" * 50)

    def _stop(self):
        self.log("⏹️ 正在停止...")
        self.running = False
        # 强行退出 pyautogui 操作
        pyautogui.FAILSAFE = False

    # ── 定时任务管理 ──
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

        max_steps = int(self.steps_var.get() or DEFAULT_MAX_STEPS)
        self.scheduler.add(task, max_steps, sched, val)
        self._refresh_task_list()
        self.log(f"✅ 已添加定时任务: {task} ({sched}: {val})")

    def _toggle_scheduled(self):
        sel = self.task_list.selection()
        if sel:
            idx = self.task_list.index(sel[0])
            self.scheduler.toggle(idx)
            self._refresh_task_list()

    def _remove_scheduled(self):
        sel = self.task_list.selection()
        if sel:
            idx = self.task_list.index(sel[0])
            self.scheduler.remove(idx)
            self._refresh_task_list()

    def _refresh_task_list(self):
        for item in self.task_list.get_children():
            self.task_list.delete(item)
        for i, t in enumerate(self.scheduler.tasks):
            status = "✅" if t["enabled"] else "❌"
            sched_desc = f"{t['schedule_type']}: {t['schedule_value']}"
            self.task_list.insert("", tk.END, iid=str(i), values=(t["task"], sched_desc, status))

    # ── 配置窗口 ──
    def _open_config(self):
        cfg_win = tk.Toplevel(self.root)
        cfg_win.title("配置")
        cfg_win.geometry("500x350")

        notebook = ttk.Notebook(cfg_win)
        notebook.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        for section in ['key', 'obs']:
            frame = ttk.Frame(notebook)
            notebook.add(frame, text=section.upper())
            entries = {}
            for i, key in enumerate(self.cfg_dict[section]):
                ttk.Label(frame, text=key).grid(row=i, column=0, sticky=tk.W, padx=5, pady=3)
                var = tk.StringVar(value=self.cfg_dict[section][key])
                ttk.Entry(frame, textvariable=var, width=50).grid(row=i, column=1, padx=5, pady=3)
                entries[key] = var

            def save_section(sec=section, ent=entries):
                for k, v in ent.items():
                    self.config_obj.set(sec, k, v.get())
                    self.cfg_dict[sec][k] = v.get()
                save_config(self.config_obj, self.config_path)
                messagebox.showinfo("提示", f"[{sec}] 配置已保存")

            ttk.Button(frame, text="保存", command=save_section).grid(
                row=len(self.cfg_dict[section]), column=0, columnspan=2, pady=10
            )

    def _on_close(self):
        self.running = False
        self.scheduler_running = False
        pyautogui.FAILSAFE = False
        self.root.destroy()

    def run(self):
        self.root.mainloop()


# ==================== 入口 ====================
if __name__ == "__main__":
    App().run()
