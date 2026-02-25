#!/usr/bin/env python3
"""音频声道切分工具 - 支持立体声左/右声道分离及单声道音频裁剪"""

import sys
import os
import subprocess
import json
from pathlib import Path

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QFileDialog, QGroupBox, QLineEdit,
    QProgressBar, QTextEdit, QFrame,
    QCheckBox, QComboBox, QMessageBox,
    QGridLayout, QStackedWidget
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QDragEnterEvent, QDropEvent


def get_audio_info(filepath: str) -> dict:
    """使用ffprobe获取音频文件信息"""
    cmd = [
        "ffprobe", "-v", "quiet",
        "-print_format", "json",
        "-show_streams", "-show_format",
        filepath
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            return {}
        data = json.loads(result.stdout)
        info = {}
        fmt = data.get("format", {})
        info["duration"] = float(fmt.get("duration", 0))
        info["size"] = int(fmt.get("size", 0))
        info["format_name"] = fmt.get("format_long_name", "Unknown")

        for stream in data.get("streams", []):
            if stream.get("codec_type") == "audio":
                info["channels"] = stream.get("channels", 0)
                info["sample_rate"] = stream.get("sample_rate", "Unknown")
                info["codec"] = stream.get("codec_name", "Unknown")
                info["bit_rate"] = int(stream.get("bit_rate", 0)) if stream.get("bit_rate") else 0
                info["channel_layout"] = stream.get("channel_layout", "Unknown")
                break
        return info
    except Exception as e:
        return {"error": str(e)}


def format_duration(seconds: float) -> str:
    """将秒数格式化为 HH:MM:SS.mmm"""
    if seconds <= 0:
        return "00:00:00.000"
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}"


def parse_time_input(text: str) -> float:
    """解析时间输入，支持 秒数 或 HH:MM:SS 格式"""
    text = text.strip()
    if not text:
        return 0.0
    # 尝试 HH:MM:SS 或 MM:SS 格式
    parts = text.split(":")
    if len(parts) == 3:
        try:
            return float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
        except ValueError:
            pass
    elif len(parts) == 2:
        try:
            return float(parts[0]) * 60 + float(parts[1])
        except ValueError:
            pass
    # 尝试纯秒数
    try:
        return float(text)
    except ValueError:
        return 0.0


class FFmpegWorker(QThread):
    """在后台线程执行ffmpeg命令"""
    progress = pyqtSignal(str)
    finished = pyqtSignal(bool, str)

    def __init__(self, commands: list[tuple[str, list[str]]]):
        super().__init__()
        self.commands = commands  # [(label, cmd), ...]
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        for label, cmd in self.commands:
            if self._cancelled:
                self.finished.emit(False, "已取消")
                return
            self.progress.emit(f">> {label}")
            try:
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True
                )
                _, stderr = proc.communicate(timeout=300)
                if self._cancelled:
                    proc.terminate()
                    self.finished.emit(False, "已取消")
                    return
                if proc.returncode != 0:
                    self.finished.emit(False, f"命令失败:\n{stderr.strip()}")
                    return
                self.progress.emit(f"  [OK] 完成: {label}")
            except subprocess.TimeoutExpired:
                self.finished.emit(False, "处理超时，请检查文件")
                return
            except Exception as e:
                self.finished.emit(False, f"执行错误: {e}")
                return
        self.finished.emit(True, "所有任务完成！")


class DropZone(QLabel):
    """可拖放文件的区域"""
    file_dropped = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setText("拖放音频文件到此处\n或点击「选择文件」按钮")
        self.setStyleSheet("""
            QLabel {
                border: 2px dashed #555;
                border-radius: 10px;
                padding: 30px;
                color: #888;
                font-size: 14px;
                background: #1e1e2e;
                min-height: 80px;
            }
            QLabel:hover {
                border-color: #7c6af7;
                color: #aaa;
            }
        """)

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            urls = event.mimeData().urls()
            if urls and self._is_audio(urls[0].toLocalFile()):
                event.acceptProposedAction()
                self.setStyleSheet(self.styleSheet().replace("#555", "#7c6af7"))

    def dragLeaveEvent(self, event):
        self.setStyleSheet(self.styleSheet().replace("#7c6af7", "#555"))

    def dropEvent(self, event: QDropEvent):
        urls = event.mimeData().urls()
        if urls:
            filepath = urls[0].toLocalFile()
            if self._is_audio(filepath):
                self.file_dropped.emit(filepath)
        self.setStyleSheet(self.styleSheet().replace("#7c6af7", "#555"))

    def _is_audio(self, path: str) -> bool:
        suffixes = {".mp3", ".wav", ".flac", ".aac", ".ogg", ".m4a", ".wma", ".opus", ".aiff", ".mp4", ".mkv", ".mov"}
        return Path(path).suffix.lower() in suffixes


class TimeInput(QWidget):
    """时间输入组件，支持 开始/结束 或 持续时长 模式"""
    def __init__(self, label: str, parent=None):
        super().__init__(parent)
        self._total_duration = 0.0
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        title = QLabel(label)
        title.setStyleSheet("color: #cdd6f4; font-weight: bold; font-size: 13px;")
        layout.addWidget(title)

        mode_layout = QHBoxLayout()
        self.mode_combo = QComboBox()
        self.mode_combo.addItems(["按起止时间裁剪", "从头保留指定时长"])
        self.mode_combo.setStyleSheet("""
            QComboBox {
                background: #313244; color: #cdd6f4; border: 1px solid #45475a;
                border-radius: 5px; padding: 4px 8px; font-size: 12px;
            }
            QComboBox::drop-down { border: none; }
            QComboBox QAbstractItemView { background: #313244; color: #cdd6f4; selection-background-color: #7c6af7; }
        """)
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        mode_layout.addWidget(self.mode_combo)
        mode_layout.addStretch()
        layout.addLayout(mode_layout)

        # 起止时间组
        self.range_widget = QWidget()
        range_layout = QGridLayout(self.range_widget)
        range_layout.setContentsMargins(0, 0, 0, 0)
        range_layout.setSpacing(6)

        range_layout.addWidget(QLabel("起始时间:"), 0, 0)
        self.start_input = QLineEdit("0")
        self.start_input.setPlaceholderText("秒 或 HH:MM:SS")
        range_layout.addWidget(self.start_input, 0, 1)

        range_layout.addWidget(QLabel("结束时间:"), 1, 0)
        self.end_input = QLineEdit()
        self.end_input.setPlaceholderText("秒 或 HH:MM:SS（留空=到末尾）")
        range_layout.addWidget(self.end_input, 1, 1)

        for lbl in self.range_widget.findChildren(QLabel):
            lbl.setStyleSheet("color: #a6adc8; font-size: 12px;")
        for inp in [self.start_input, self.end_input]:
            inp.setStyleSheet("""
                QLineEdit { background: #313244; color: #cdd6f4; border: 1px solid #45475a;
                border-radius: 5px; padding: 4px 8px; font-size: 12px; }
                QLineEdit:focus { border-color: #7c6af7; }
            """)
        layout.addWidget(self.range_widget)

        # 时长模式组
        self.duration_widget = QWidget()
        dur_layout = QHBoxLayout(self.duration_widget)
        dur_layout.setContentsMargins(0, 0, 0, 0)
        dur_layout.setSpacing(6)
        dur_lbl = QLabel("保留时长:")
        dur_lbl.setStyleSheet("color: #a6adc8; font-size: 12px;")
        self.duration_input = QLineEdit()
        self.duration_input.setPlaceholderText("秒 或 HH:MM:SS（留空=全部）")
        self.duration_input.setStyleSheet("""
            QLineEdit { background: #313244; color: #cdd6f4; border: 1px solid #45475a;
            border-radius: 5px; padding: 4px 8px; font-size: 12px; }
            QLineEdit:focus { border-color: #7c6af7; }
        """)
        dur_layout.addWidget(dur_lbl)
        dur_layout.addWidget(self.duration_input)
        self.duration_widget.hide()
        layout.addWidget(self.duration_widget)

    def _on_mode_changed(self, idx: int):
        if idx == 0:
            self.range_widget.show()
            self.duration_widget.hide()
        else:
            self.range_widget.hide()
            self.duration_widget.show()

    def set_total_duration(self, dur: float):
        self._total_duration = dur
        if not self.end_input.text():
            self.end_input.setPlaceholderText(f"秒 或 HH:MM:SS（留空=到末尾 {format_duration(dur)}）")

    def get_ffmpeg_args(self) -> list[str]:
        """返回 ffmpeg -ss/-to/-t 参数列表"""
        mode = self.mode_combo.currentIndex()
        args = []
        if mode == 0:
            start = parse_time_input(self.start_input.text())
            end_text = self.end_input.text().strip()
            if start > 0:
                args += ["-ss", str(start)]
            if end_text:
                end = parse_time_input(end_text)
                if end > start:
                    args += ["-to", str(end)]
        else:
            dur_text = self.duration_input.text().strip()
            if dur_text:
                dur = parse_time_input(dur_text)
                if dur > 0:
                    args += ["-t", str(dur)]
        return args

    def is_trimming(self) -> bool:
        """是否有裁剪参数"""
        return bool(self.get_ffmpeg_args())


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("音频声道切分工具")
        self.setMinimumSize(820, 700)
        self.resize(900, 760)
        self._input_file = ""
        self._audio_info = {}
        self._worker = None
        self._is_mono = False
        self._setup_style()
        self._build_ui()

    def _setup_style(self):
        self.setStyleSheet("""
            QMainWindow { background: #1e1e2e; }
            QWidget { background: #1e1e2e; color: #cdd6f4; font-family: "PingFang SC", "Helvetica Neue", Arial; }
            QGroupBox {
                border: 1px solid #45475a;
                border-radius: 8px;
                margin-top: 12px;
                padding-top: 8px;
                font-weight: bold;
                font-size: 13px;
                color: #cba6f7;
            }
            QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 6px; }
            QPushButton {
                background: #7c6af7; color: white; border: none;
                border-radius: 6px; padding: 8px 18px; font-size: 13px; font-weight: bold;
            }
            QPushButton:hover { background: #9580fa; }
            QPushButton:pressed { background: #5c4dc0; }
            QPushButton:disabled { background: #45475a; color: #6c7086; }
            QProgressBar {
                border: 1px solid #45475a; border-radius: 6px;
                background: #313244; text-align: center; color: #cdd6f4;
                height: 20px;
            }
            QProgressBar::chunk { background: #7c6af7; border-radius: 5px; }
            QTextEdit {
                background: #11111b; color: #a6e3a1; border: 1px solid #45475a;
                border-radius: 6px; font-family: 'Menlo', 'Courier New', monospace; font-size: 12px;
                padding: 6px;
            }
            QScrollBar:vertical { background: #1e1e2e; width: 8px; }
            QScrollBar::handle:vertical { background: #45475a; border-radius: 4px; }
        """)

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(16, 12, 16, 12)
        root.setSpacing(12)

        # 标题栏
        title_label = QLabel("音频声道切分工具")
        title_label.setStyleSheet("color: #cba6f7; font-size: 20px; font-weight: bold; padding: 4px 0;")
        root.addWidget(title_label)

        subtitle = QLabel("立体声：拆分左/右声道并裁剪  |  单声道：直接裁剪保留片段")
        subtitle.setStyleSheet("color: #6c7086; font-size: 12px;")
        root.addWidget(subtitle)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("color: #313244;")
        root.addWidget(sep)

        # 文件选择区
        file_group = QGroupBox("输入文件")
        file_layout = QVBoxLayout(file_group)
        file_layout.setSpacing(8)

        self.drop_zone = DropZone()
        self.drop_zone.file_dropped.connect(self._load_file)
        file_layout.addWidget(self.drop_zone)

        file_btn_layout = QHBoxLayout()
        self.select_btn = QPushButton("选择音频文件")
        self.select_btn.clicked.connect(self._choose_file)
        self.select_btn.setStyleSheet(self.select_btn.styleSheet() + "min-width: 150px;")
        file_btn_layout.addWidget(self.select_btn)
        file_btn_layout.addStretch()
        file_layout.addLayout(file_btn_layout)

        # 文件信息
        self.info_label = QLabel("尚未选择文件")
        self.info_label.setStyleSheet("""
            color: #89dceb; font-size: 12px;
            background: #181825; border-radius: 6px; padding: 8px 12px;
        """)
        self.info_label.setWordWrap(True)
        file_layout.addWidget(self.info_label)

        root.addWidget(file_group)

        # 声道设置区（用 QStackedWidget 在单声道/立体声之间切换）
        self.channels_group = QGroupBox("声道裁剪设置")
        channels_outer = QVBoxLayout(self.channels_group)
        channels_outer.setContentsMargins(8, 8, 8, 8)

        self.channel_stack = QStackedWidget()

        # 页 0：立体声 — 左右并排
        stereo_page = QWidget()
        stereo_layout = QHBoxLayout(stereo_page)
        stereo_layout.setContentsMargins(0, 0, 0, 0)
        stereo_layout.setSpacing(16)
        self.left_channel = self._make_channel_box("左声道 (Left / Channel 0)", "#a6e3a1")
        self.right_channel = self._make_channel_box("右声道 (Right / Channel 1)", "#89b4fa")
        stereo_layout.addWidget(self.left_channel["box"])
        stereo_layout.addWidget(self.right_channel["box"])

        # 页 1：单声道 — 单个裁剪面板
        mono_page = QWidget()
        mono_layout = QHBoxLayout(mono_page)
        mono_layout.setContentsMargins(0, 0, 0, 0)
        self.mono_channel = self._make_mono_box()
        mono_layout.addWidget(self.mono_channel["box"])
        mono_layout.addStretch()

        self.channel_stack.addWidget(stereo_page)   # index 0
        self.channel_stack.addWidget(mono_page)     # index 1
        channels_outer.addWidget(self.channel_stack)
        root.addWidget(self.channels_group)

        # 输出设置
        output_group = QGroupBox("输出设置")
        output_layout = QGridLayout(output_group)
        output_layout.setSpacing(8)

        output_layout.addWidget(QLabel("输出目录:"), 0, 0)
        dir_row = QHBoxLayout()
        self.output_dir_input = QLineEdit()
        self.output_dir_input.setPlaceholderText("默认与输入文件同目录")
        self.output_dir_input.setStyleSheet("""
            QLineEdit { background: #313244; color: #cdd6f4; border: 1px solid #45475a;
            border-radius: 5px; padding: 4px 8px; font-size: 12px; }
            QLineEdit:focus { border-color: #7c6af7; }
        """)
        dir_row.addWidget(self.output_dir_input)
        browse_btn = QPushButton("浏览")
        browse_btn.setStyleSheet("QPushButton { min-width:60px; padding: 5px 10px; font-size:12px; } QPushButton:hover { background:#9580fa; }")
        browse_btn.clicked.connect(self._choose_output_dir)
        dir_row.addWidget(browse_btn)
        output_layout.addLayout(dir_row, 0, 1)

        output_layout.addWidget(QLabel("输出格式:"), 1, 0)
        fmt_row = QHBoxLayout()
        self.format_combo = QComboBox()
        self.format_combo.addItems(["与源文件相同", "WAV", "MP3", "FLAC", "AAC (m4a)", "OGG"])
        self.format_combo.setStyleSheet("""
            QComboBox { background: #313244; color: #cdd6f4; border: 1px solid #45475a;
            border-radius: 5px; padding: 4px 8px; font-size: 12px; min-width: 150px; }
            QComboBox::drop-down { border: none; }
            QComboBox QAbstractItemView { background: #313244; color: #cdd6f4; selection-background-color: #7c6af7; }
        """)
        fmt_row.addWidget(self.format_combo)
        fmt_row.addStretch()
        output_layout.addLayout(fmt_row, 1, 1)

        for lbl in output_group.findChildren(QLabel):
            lbl.setStyleSheet("color: #a6adc8; font-size: 12px;")

        root.addWidget(output_group)

        # 操作按钮
        btn_layout = QHBoxLayout()
        self.process_btn = QPushButton("开始处理")
        self.process_btn.setEnabled(False)
        self.process_btn.setStyleSheet("""
            QPushButton { background: #40a02b; font-size: 14px; padding: 10px 30px; }
            QPushButton:hover { background: #4ec637; }
            QPushButton:disabled { background: #45475a; color: #6c7086; }
        """)
        self.process_btn.clicked.connect(self._start_processing)

        self.cancel_btn = QPushButton("取消")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.setStyleSheet("""
            QPushButton { background: #e64553; font-size: 14px; padding: 10px 24px; }
            QPushButton:hover { background: #f25060; }
            QPushButton:disabled { background: #45475a; color: #6c7086; }
        """)
        self.cancel_btn.clicked.connect(self._cancel_processing)

        btn_layout.addStretch()
        btn_layout.addWidget(self.process_btn)
        btn_layout.addWidget(self.cancel_btn)
        btn_layout.addStretch()
        root.addLayout(btn_layout)

        # 进度和日志
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setVisible(False)
        root.addWidget(self.progress_bar)

        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setMaximumHeight(130)
        self.log_text.setPlaceholderText("处理日志将显示在这里...")
        root.addWidget(self.log_text)

    def _make_channel_box(self, title: str, color: str) -> dict:
        box = QGroupBox(title)
        box.setStyleSheet(f"QGroupBox {{ color: {color}; border-color: #45475a; }}")
        layout = QVBoxLayout(box)
        layout.setSpacing(8)

        enabled_cb = QCheckBox("导出此声道")
        enabled_cb.setChecked(True)
        enabled_cb.setStyleSheet("color: #cdd6f4; font-size: 12px;")
        layout.addWidget(enabled_cb)

        time_input = TimeInput("裁剪范围")
        layout.addWidget(time_input)

        suffix_layout = QHBoxLayout()
        suffix_lbl = QLabel("文件后缀:")
        suffix_lbl.setStyleSheet("color: #a6adc8; font-size: 12px;")
        suffix_input = QLineEdit("_left" if "Left" in title else "_right")
        suffix_input.setStyleSheet("""
            QLineEdit { background: #313244; color: #cdd6f4; border: 1px solid #45475a;
            border-radius: 5px; padding: 4px 8px; font-size: 12px; max-width: 120px; }
            QLineEdit:focus { border-color: #7c6af7; }
        """)
        suffix_layout.addWidget(suffix_lbl)
        suffix_layout.addWidget(suffix_input)
        suffix_layout.addStretch()
        layout.addLayout(suffix_layout)
        layout.addStretch()

        return {"box": box, "enabled": enabled_cb, "time": time_input, "suffix": suffix_input}

    def _make_mono_box(self) -> dict:
        """单声道裁剪面板"""
        box = QGroupBox("单声道 (Mono)")
        box.setStyleSheet("QGroupBox { color: #f9e2af; border-color: #45475a; }")
        layout = QVBoxLayout(box)
        layout.setSpacing(8)

        hint = QLabel("检测到单声道音频，可直接设置裁剪范围后导出")
        hint.setStyleSheet("color: #6c7086; font-size: 11px;")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        time_input = TimeInput("裁剪范围")
        layout.addWidget(time_input)

        suffix_layout = QHBoxLayout()
        suffix_lbl = QLabel("文件后缀:")
        suffix_lbl.setStyleSheet("color: #a6adc8; font-size: 12px;")
        suffix_input = QLineEdit("_trimmed")
        suffix_input.setStyleSheet("""
            QLineEdit { background: #313244; color: #cdd6f4; border: 1px solid #45475a;
            border-radius: 5px; padding: 4px 8px; font-size: 12px; max-width: 140px; }
            QLineEdit:focus { border-color: #7c6af7; }
        """)
        suffix_layout.addWidget(suffix_lbl)
        suffix_layout.addWidget(suffix_input)
        suffix_layout.addStretch()
        layout.addLayout(suffix_layout)
        layout.addStretch()

        return {"box": box, "time": time_input, "suffix": suffix_input}

    def _choose_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "选择音频文件", "",
            "音频文件 (*.mp3 *.wav *.flac *.aac *.ogg *.m4a *.wma *.opus *.aiff);;所有文件 (*.*)"
        )
        if path:
            self._load_file(path)

    def _choose_output_dir(self):
        path = QFileDialog.getExistingDirectory(self, "选择输出目录")
        if path:
            self.output_dir_input.setText(path)

    def _load_file(self, filepath: str):
        self._input_file = filepath
        self.drop_zone.setText(f"已选择: {Path(filepath).name}")
        self.drop_zone.setStyleSheet(self.drop_zone.styleSheet().replace(
            "color: #888", "color: #a6e3a1"
        ).replace("border: 2px dashed #555", "border: 2px solid #40a02b"))

        self._audio_info = get_audio_info(filepath)
        self._update_info_display()
        self.process_btn.setEnabled(True)

        dur = self._audio_info.get("duration", 0)
        channels = self._audio_info.get("channels", 2)
        self._is_mono = (channels == 1)

        if self._is_mono:
            # 切换到单声道面板
            self.channel_stack.setCurrentIndex(1)
            self.channels_group.setTitle("裁剪设置（单声道）")
            self.mono_channel["time"].set_total_duration(dur)
        else:
            # 切换到立体声面板
            self.channel_stack.setCurrentIndex(0)
            self.channels_group.setTitle("声道裁剪设置（立体声）")
            self.left_channel["time"].set_total_duration(dur)
            self.right_channel["time"].set_total_duration(dur)

    def _update_info_display(self):
        info = self._audio_info
        if "error" in info:
            self.info_label.setText(f"[错误] 读取失败: {info['error']}")
            return
        if not info:
            self.info_label.setText("[错误] 无法读取文件信息")
            return

        dur = info.get("duration", 0)
        ch = info.get("channels", "?")
        sr = info.get("sample_rate", "?")
        codec = info.get("codec", "?")
        fmt = info.get("format_name", "?")
        size_mb = info.get("size", 0) / 1024 / 1024
        br = info.get("bit_rate", 0)
        br_str = f"{br // 1000} kbps" if br else "N/A"

        ch_warn = ""
        if ch == 1:
            ch_warn = "  — <b>单声道模式</b>，将直接裁剪导出"
        elif ch != 2:
            ch_warn = f"  [注意] 非标准声道数({ch})，结果可能不符预期"

        self.info_label.setText(
            f"<b>文件:</b> {Path(self._input_file).name} &nbsp;|&nbsp; "
            f"<b>时长:</b> {format_duration(dur)} &nbsp;|&nbsp; "
            f"<b>声道数:</b> {ch}{ch_warn}<br>"
            f"<b>采样率:</b> {sr} Hz &nbsp;|&nbsp; "
            f"<b>编码:</b> {codec} &nbsp;|&nbsp; "
            f"<b>格式:</b> {fmt} &nbsp;|&nbsp; "
            f"<b>码率:</b> {br_str} &nbsp;|&nbsp; "
            f"<b>大小:</b> {size_mb:.2f} MB"
        )
        self.info_label.setTextFormat(Qt.TextFormat.RichText)

    def _get_output_ext(self) -> str:
        fmt_map = {
            "WAV": ".wav",
            "MP3": ".mp3",
            "FLAC": ".flac",
            "AAC (m4a)": ".m4a",
            "OGG": ".ogg",
        }
        sel = self.format_combo.currentText()
        if sel == "与源文件相同":
            return Path(self._input_file).suffix.lower()
        return fmt_map.get(sel, ".wav")

    def _build_commands(self) -> list[tuple[str, list[str]]]:
        if not self._input_file:
            return []

        out_dir = self.output_dir_input.text().strip()
        if not out_dir:
            out_dir = str(Path(self._input_file).parent)

        stem = Path(self._input_file).stem
        ext = self._get_output_ext()
        commands = []

        if self._is_mono:
            return self._build_mono_command(stem, ext, out_dir)

        # ── 立体声：逐声道提取 ──
        channel_configs = [
            (self.left_channel, 0, "左声道"),
            (self.right_channel, 1, "右声道"),
        ]
        for cfg, ch_idx, ch_name in channel_configs:
            if not cfg["enabled"].isChecked():
                continue
            suffix = cfg["suffix"].text().strip() or ("_left" if ch_idx == 0 else "_right")
            time_args = cfg["time"].get_ffmpeg_args()
            output_path = os.path.join(out_dir, f"{stem}{suffix}{ext}")

            pan_filter = f"pan=mono|c0=c{ch_idx}"
            cmd = ["ffmpeg", "-y", "-i", self._input_file]
            cmd += time_args
            cmd += ["-af", pan_filter]
            cmd += self._codec_args(ext)
            cmd.append(output_path)
            commands.append((f"导出{ch_name} → {Path(output_path).name}", cmd))

        return commands

    def _build_mono_command(self, stem: str, ext: str, out_dir: str) -> list[tuple[str, list[str]]]:
        """单声道裁剪命令"""
        suffix = self.mono_channel["suffix"].text().strip() or "_trimmed"
        time_args = self.mono_channel["time"].get_ffmpeg_args()
        output_path = os.path.join(out_dir, f"{stem}{suffix}{ext}")

        cmd = ["ffmpeg", "-y", "-i", self._input_file]
        cmd += time_args
        cmd += self._codec_args(ext)
        cmd.append(output_path)
        return [(f"导出单声道裁剪 → {Path(output_path).name}", cmd)]

    def _codec_args(self, ext: str) -> list[str]:
        """根据输出格式返回编解码器参数"""
        if ext == ".mp3":
            return ["-codec:a", "libmp3lame", "-q:a", "2"]
        if ext == ".flac":
            return ["-codec:a", "flac"]
        if ext == ".m4a":
            return ["-codec:a", "aac", "-b:a", "192k"]
        if ext == ".ogg":
            return ["-codec:a", "libvorbis", "-q:a", "6"]
        if ext == ".wav":
            return ["-codec:a", "pcm_s16le"]
        return []

    def _start_processing(self):
        if not self._input_file:
            QMessageBox.warning(self, "提示", "请先选择输入文件！")
            return

        if not self._is_mono:
            if not self.left_channel["enabled"].isChecked() and not self.right_channel["enabled"].isChecked():
                QMessageBox.warning(self, "提示", "请至少勾选一个声道进行导出！")
                return

        commands = self._build_commands()
        if not commands:
            QMessageBox.warning(self, "提示", "没有可执行的任务！")
            return

        # 确认输出路径
        out_dir = self.output_dir_input.text().strip()
        if not out_dir:
            out_dir = str(Path(self._input_file).parent)
        if not os.path.isdir(out_dir):
            try:
                os.makedirs(out_dir)
            except OSError as e:
                QMessageBox.critical(self, "错误", f"无法创建输出目录: {e}")
                return

        self.log_text.clear()
        self._log(f"开始处理: {Path(self._input_file).name}")
        self._log(f"输出目录: {out_dir}")
        self._log(f"任务数: {len(commands)}")
        self._log("─" * 50)

        self.process_btn.setEnabled(False)
        self.cancel_btn.setEnabled(True)
        self.progress_bar.setVisible(True)

        self._worker = FFmpegWorker(commands)
        self._worker.progress.connect(self._log)
        self._worker.finished.connect(self._on_finished)
        self._worker.start()

    def _cancel_processing(self):
        if self._worker and self._worker.isRunning():
            self._worker.cancel()
            self._log("[停止] 正在取消...")

    def _on_finished(self, success: bool, message: str):
        self.progress_bar.setVisible(False)
        self.process_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)

        self._log("─" * 50)
        if success:
            self._log(f"[完成] {message}")
            QMessageBox.information(self, "完成", f"处理完成！\n{message}")
        else:
            self._log(f"[错误] {message}")
            if "取消" not in message:
                QMessageBox.critical(self, "处理失败", message)

    def _log(self, text: str):
        self.log_text.append(text)
        self.log_text.verticalScrollBar().setValue(
            self.log_text.verticalScrollBar().maximum()
        )


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("音频声道切分工具")

    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
