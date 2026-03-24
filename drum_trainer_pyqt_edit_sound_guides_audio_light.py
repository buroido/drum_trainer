import math
import statistics
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import tempfile
import wave
import struct
import zipfile
import shutil

from PyQt5.QtCore import Qt, QTimer, QRectF, pyqtSignal, QObject
from PyQt5.QtGui import QColor, QPainter, QPen, QFont
from PyQt5.QtWidgets import (
    QApplication,
    QComboBox,
    QDoubleSpinBox,
    QListWidget,
    QListWidgetItem,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

try:
    import mido
except Exception:
    mido = None


try:
    from PyQt5.QtCore import QUrl
    from PyQt5.QtMultimedia import QSoundEffect
except Exception:
    QUrl = None
    QSoundEffect = None

BASE_LANES = ["Kick", "Snare", "Hi-Hat", "Tom", "Crash"]
DEFAULT_MIDI_MAP = {
    36: "Kick",
    35: "Kick",
    38: "Snare",
    40: "Snare",
    42: "Hi-Hat",
    46: "Hi-Hat",
    45: "Tom",
    47: "Tom",
    49: "Crash",
    57: "Crash",
}
KEYBOARD_FALLBACK_MAP = {
    Qt.Key_A: "Kick",
    Qt.Key_S: "Snare",
    Qt.Key_D: "Hi-Hat",
    Qt.Key_F: "Tom",
    Qt.Key_G: "Crash",
}


@dataclass
class GridNote:
    lane: str
    bar: int
    step: int


@dataclass
class TimedNote:
    lane: str
    time_sec: float
    bar: int
    step: int


@dataclass
class HitEvent:
    lane: str
    time_sec: float
    velocity: int = 100
    source: str = "midi"


class ToneLibrary:
    def __init__(self) -> None:
        self.base_dir = Path(tempfile.gettempdir()) / "drum_trainer_sounds"
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.sample_rate = 44100

    def ensure_sound(self, name: str, freq: float, duration: float, volume: float = 0.45, wave_kind: str = "sine") -> str:
        path = self.base_dir / f"{name}.wav"
        if path.exists():
            return str(path)
        frames = int(self.sample_rate * duration)
        with wave.open(str(path), "w") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(self.sample_rate)
            for i in range(frames):
                t = i / self.sample_rate
                env = max(0.0, 1.0 - (t / duration)) ** 2
                if wave_kind == "square":
                    raw = 1.0 if math.sin(2 * math.pi * freq * t) >= 0 else -1.0
                elif wave_kind == "kick":
                    sweep = max(35.0, freq * (1.0 - 0.92 * (t / duration)))
                    body = math.sin(2 * math.pi * sweep * t)
                    click = math.sin(2 * math.pi * 1800 * t) * max(0.0, 1.0 - t / 0.012)
                    raw = body * 0.92 + click * 0.15
                elif wave_kind == "snare":
                    tone = math.sin(2 * math.pi * freq * t) * 0.25
                    noise = (math.sin(2 * math.pi * 4100 * t) * math.sin(2 * math.pi * 2960 * t)) * 0.95
                    raw = tone + noise
                elif wave_kind == "hihat":
                    raw = 0.6 * math.sin(2 * math.pi * 7000 * t) + 0.4 * math.sin(2 * math.pi * 9010 * t)
                elif wave_kind == "crash":
                    raw = (0.45 * math.sin(2 * math.pi * 2800 * t) + 0.35 * math.sin(2 * math.pi * 4120 * t) + 0.25 * math.sin(2 * math.pi * 5330 * t))
                else:
                    raw = math.sin(2 * math.pi * freq * t)
                sample = int(max(-32767, min(32767, raw * env * volume * 32767)))
                wf.writeframes(struct.pack("<h", sample))
        return str(path)


class AudioEngine(QObject):
    SAMPLE_ALIASES = {
        "Kick": ["kick", "bd", "bass_drum"],
        "Snare": ["snare", "sd"],
        "Hi-Hat": ["hihat", "hihat_closed", "hh", "closed_hat"],
        "Tom": ["tom", "tom_mid", "tom1"],
        "Crash": ["crash", "cymbal_crash", "crash1"],
        "metronome_major": ["metronome_major", "click_major", "metro_major"],
        "metronome_minor": ["metronome_minor", "click_minor", "metro_minor"],
    }

    def __init__(self) -> None:
        super().__init__()
        self.enabled = QSoundEffect is not None and QUrl is not None
        self.tone_library = ToneLibrary()
        self.base_dir = self.tone_library.base_dir
        self.sound_paths = self._prepare_builtin_sound_paths()
        self.sound_pool: Dict[str, List[object]] = {}
        self.pool_index: Dict[str, int] = {}
        self.pool_sizes: Dict[str, int] = {
            "Kick": 10,
            "Snare": 10,
            "Hi-Hat": 12,
            "Tom": 8,
            "Crash": 6,
            "metronome_major": 4,
            "metronome_minor": 4,
        }
        if self.enabled:
            self._create_pool()

    def _prepare_builtin_sound_paths(self) -> Dict[str, str]:
        return {
            "Kick": self.tone_library.ensure_sound("kick_builtin", 78, 0.18, 0.85, "kick"),
            "Snare": self.tone_library.ensure_sound("snare_builtin", 210, 0.12, 0.62, "snare"),
            "Hi-Hat": self.tone_library.ensure_sound("hihat_builtin", 8200, 0.05, 0.34, "hihat"),
            "Tom": self.tone_library.ensure_sound("tom_builtin", 145, 0.16, 0.65, "sine"),
            "Crash": self.tone_library.ensure_sound("crash_builtin", 3400, 0.45, 0.40, "crash"),
            "metronome_major": self.tone_library.ensure_sound("metro_major_builtin", 1500, 0.05, 0.55, "square"),
            "metronome_minor": self.tone_library.ensure_sound("metro_minor_builtin", 1100, 0.04, 0.35, "square"),
        }

    def _create_pool(self) -> None:
        self.sound_pool.clear()
        self.pool_index.clear()
        for key, path in self.sound_paths.items():
            pool = []
            voice_count = self.pool_sizes.get(key, 6)
            for _ in range(voice_count):
                snd = QSoundEffect()
                snd.setSource(QUrl.fromLocalFile(path))
                snd.setLoopCount(1)
                snd.setVolume(0.95)
                pool.append(snd)
            self.sound_pool[key] = pool
            self.pool_index[key] = 0

    def _find_sample(self, base_dir: Path, aliases: List[str]) -> Optional[Path]:
        for path in base_dir.rglob("*.wav"):
            lower = path.stem.lower()
            if lower in aliases or any(alias in lower for alias in aliases):
                return path
        return None

    def _load_from_dir(self, folder: Path) -> Tuple[bool, List[str], List[str]]:
        loaded: List[str] = []
        missing: List[str] = []
        new_paths = dict(self._prepare_builtin_sound_paths())
        for key, aliases in self.SAMPLE_ALIASES.items():
            found = self._find_sample(folder, aliases)
            if found is not None:
                new_paths[key] = str(found)
                loaded.append(f"{key}: {found.name}")
            else:
                missing.append(key)
        self.sound_paths = new_paths
        if self.enabled:
            self._create_pool()
        return True, loaded, missing

    def load_sample_source(self, source_path: str) -> Tuple[bool, List[str], List[str]]:
        source = Path(source_path)
        if not source.exists():
            return False, [], ["ファイルまたはフォルダが見つかりません"]
        if source.is_dir():
            return self._load_from_dir(source)
        if source.suffix.lower() != ".zip":
            return False, [], ["zip か フォルダを指定してください"]
        extract_root = self.base_dir / "sample_pack_unzipped"
        if extract_root.exists():
            shutil.rmtree(extract_root, ignore_errors=True)
        extract_root.mkdir(parents=True, exist_ok=True)
        try:
            with zipfile.ZipFile(str(source), "r") as zf:
                zf.extractall(str(extract_root))
        except Exception as e:
            return False, [], [f"zip の展開に失敗しました: {e}"]
        return self._load_from_dir(extract_root)

    def play(self, name: str) -> None:
        if not self.enabled:
            QApplication.beep()
            return
        pool = self.sound_pool.get(name)
        if not pool:
            return

        chosen = None
        start_index = self.pool_index.get(name, 0)
        for offset in range(len(pool)):
            snd = pool[(start_index + offset) % len(pool)]
            # Prefer a voice that is currently idle to avoid cutting off
            # the previous hit of the same instrument.
            if not snd.isPlaying():
                chosen = snd
                self.pool_index[name] = (start_index + offset + 1) % len(pool)
                break

        if chosen is None:
            # All voices are busy. Reuse in round-robin order without an
            # explicit stop(); play() on a recycled voice is less disruptive
            # than always stopping the most recent one first.
            idx = self.pool_index.get(name, 0)
            chosen = pool[idx]
            self.pool_index[name] = (idx + 1) % len(pool)

        chosen.play()


class MidiInputManager(QObject):
    hit_received = pyqtSignal(str, float, int, str)
    ports_changed = pyqtSignal(list)

    def __init__(self) -> None:
        super().__init__()
        self._port_name: Optional[str] = None
        self._port = None
        self._poll_timer = QTimer(self)
        self._poll_timer.timeout.connect(self._poll_messages)
        self._poll_timer.start(4)

    def available_ports(self) -> List[str]:
        if mido is None:
            return []
        try:
            return list(mido.get_input_names())
        except Exception:
            return []

    def refresh_ports(self) -> None:
        self.ports_changed.emit(self.available_ports())

    def open_port(self, port_name: str) -> bool:
        self.close_port()
        if not port_name or port_name == "Keyboard Fallback":
            self._port_name = port_name
            return True
        if mido is None:
            return False
        try:
            self._port = mido.open_input(port_name)
            self._port_name = port_name
            return True
        except Exception:
            self._port = None
            self._port_name = None
            return False

    def close_port(self) -> None:
        if self._port is not None:
            try:
                self._port.close()
            except Exception:
                pass
        self._port = None

    def _poll_messages(self) -> None:
        if self._port is None:
            return
        try:
            for msg in self._port.iter_pending():
                if msg.type == "note_on" and getattr(msg, "velocity", 0) > 0:
                    lane = DEFAULT_MIDI_MAP.get(getattr(msg, "note", -1))
                    if lane is not None:
                        self.hit_received.emit(lane, time.perf_counter(), int(msg.velocity), "midi")
        except Exception:
            pass

    def emit_keyboard_hit(self, lane: str) -> None:
        self.hit_received.emit(lane, time.perf_counter(), 100, "keyboard")


class PatternModel(QObject):
    pattern_changed = pyqtSignal()

    def __init__(self) -> None:
        super().__init__()
        self.bpm = 120
        self.beats_per_bar = 4
        self.bars = 2
        self.subdivision = 4
        self.lanes: List[str] = list(BASE_LANES)
        self.notes: List[GridNote] = []

    @property
    def steps_per_bar(self) -> int:
        return self.beats_per_bar * self.subdivision

    @property
    def total_steps(self) -> int:
        return self.steps_per_bar * self.bars

    @property
    def step_duration(self) -> float:
        return 60.0 / self.bpm / self.subdivision

    @property
    def loop_duration(self) -> float:
        return self.step_duration * self.total_steps

    def set_bpm(self, bpm: int) -> None:
        self.bpm = bpm
        self.pattern_changed.emit()

    def set_bars(self, bars: int) -> None:
        self.bars = bars
        self.notes = [n for n in self.notes if n.bar < bars]
        self.pattern_changed.emit()

    def toggle_note(self, lane: str, bar: int, step: int) -> None:
        for i, note in enumerate(self.notes):
            if note.lane == lane and note.bar == bar and note.step == step:
                del self.notes[i]
                self.pattern_changed.emit()
                return
        self.notes.append(GridNote(lane, bar, step))
        self.notes.sort(key=lambda n: (n.bar, n.step, self.lanes.index(n.lane)))
        self.pattern_changed.emit()

    def has_note(self, lane: str, bar: int, step: int) -> bool:
        return any(n.lane == lane and n.bar == bar and n.step == step for n in self.notes)

    def set_lane_order(self, lanes: List[str]) -> None:
        if sorted(lanes) != sorted(BASE_LANES):
            return
        self.lanes = list(lanes)
        self.notes.sort(key=lambda n: (n.bar, n.step, self.lanes.index(n.lane)))
        self.pattern_changed.emit()

    def timed_notes(self) -> List[TimedNote]:
        result: List[TimedNote] = []
        for note in self.notes:
            absolute_step = note.bar * self.steps_per_bar + note.step
            result.append(TimedNote(note.lane, absolute_step * self.step_duration, note.bar, note.step))
        result.sort(key=lambda x: (x.time_sec, self.lanes.index(x.lane)))
        return result


class PracticeSession(QObject):
    session_updated = pyqtSignal()
    session_reset = pyqtSignal()

    def __init__(self, model: PatternModel) -> None:
        super().__init__()
        self.model = model
        self.is_playing = False
        self.start_time = 0.0
        self.reference_hits: List[TimedNote] = []
        self.user_hits: List[HitEvent] = []
        self.current_loop_index = 0
        self.scroll_window_sec = 3.2
        self.calibration_offset_sec = 0.0
        self._emitted_reference_ids: set = set()

    def start(self) -> None:
        self.reference_hits = self.model.timed_notes()
        self.user_hits = []
        self.current_loop_index = 0
        self._emitted_reference_ids.clear()
        self.start_time = time.perf_counter()
        self.is_playing = True
        self.session_reset.emit()
        self.session_updated.emit()

    def stop(self) -> None:
        self.is_playing = False
        self.session_updated.emit()

    def register_hit(self, lane: str, when: float, velocity: int = 100, source: str = "midi") -> None:
        if not self.is_playing:
            return
        t = when - self.start_time
        if t < 0:
            return
        self.user_hits.append(HitEvent(lane=lane, time_sec=t, velocity=velocity, source=source))
        self.session_updated.emit()

    def elapsed(self) -> float:
        return max(0.0, time.perf_counter() - self.start_time) if self.is_playing else 0.0

    def expected_hits_for_view(self) -> List[TimedNote]:
        if not self.reference_hits:
            return []
        loop_duration = self.model.loop_duration
        now_abs = self.elapsed()
        current_loop = int(now_abs // loop_duration) if loop_duration > 0 else 0
        notes: List[TimedNote] = []
        for loop in range(max(0, current_loop - 1), current_loop + 2):
            for n in self.reference_hits:
                notes.append(TimedNote(n.lane, n.time_sec + loop * loop_duration, n.bar, n.step))
        return notes

    def recent_user_hits(self) -> List[HitEvent]:
        now = self.elapsed()
        before = now - self.scroll_window_sec
        after = now + self.scroll_window_sec
        return [h for h in self.user_hits if before <= h.time_sec <= after]

    def evaluate(self) -> List[Tuple[TimedNote, Optional[HitEvent], Optional[float]]]:
        expected_abs: List[TimedNote] = []
        loop_duration = self.model.loop_duration
        if loop_duration <= 0 or not self.reference_hits:
            return []
        max_time = max(self.user_hits[-1].time_sec, self.elapsed()) if self.user_hits else self.elapsed()
        loop_count = max(1, int(math.ceil(max_time / loop_duration)) + 1)
        for loop_idx in range(loop_count):
            for n in self.reference_hits:
                expected_abs.append(TimedNote(n.lane, n.time_sec + loop_idx * loop_duration, n.bar, n.step))
        unmatched = list(self.user_hits)
        results = []
        tolerance = 0.20
        for exp in expected_abs:
            candidates = []
            for h in unmatched:
                if h.lane != exp.lane:
                    continue
                corrected = h.time_sec - self.calibration_offset_sec
                err = corrected - exp.time_sec
                if abs(err) <= tolerance:
                    candidates.append((abs(err), err, h))
            if candidates:
                _, err, best = min(candidates, key=lambda x: x[0])
                unmatched.remove(best)
                results.append((exp, best, err))
            else:
                results.append((exp, None, None))
        return results

    def summary(self) -> Dict[str, Optional[float]]:
        matched_errors = [err for _, hit, err in self.evaluate() if hit is not None and err is not None]
        if not matched_errors:
            return {"mean_ms": None, "std_ms": None, "count": 0}
        ms = [e * 1000.0 for e in matched_errors]
        return {
            "mean_ms": statistics.mean(ms),
            "std_ms": statistics.pstdev(ms) if len(ms) > 1 else 0.0,
            "count": len(ms),
        }


class LanePanel(QWidget):
    note_clicked = pyqtSignal(str, int, int)

    def __init__(self, model: PatternModel, session: PracticeSession, mode: str) -> None:
        super().__init__()
        self.model = model
        self.session = session
        self.mode = mode
        self.setMinimumSize(480, 720)
        self.setFocusPolicy(Qt.StrongFocus)
        self.margin_left = 28
        self.margin_right = 18
        self.margin_top = 28
        self.margin_bottom = 28
        self.judge_line_y_ratio = 0.78
        self.scroll_speed = 180.0
        self.guides_enabled = True
        self.guide_division = 4  # 4, 8, 16
        self.model.pattern_changed.connect(self.update)
        self.session.session_updated.connect(self.update)
        self.session.session_reset.connect(self.update)

    def sizeHint(self):
        return self.minimumSize()

    def lane_width(self) -> float:
        return (self.width() - self.margin_left - self.margin_right) / len(self.model.lanes)

    def grid_height(self) -> float:
        return self.height() - self.margin_top - self.margin_bottom

    def judge_line_y(self) -> float:
        return self.margin_top + self.grid_height() * self.judge_line_y_ratio

    def set_guides_enabled(self, enabled: bool) -> None:
        self.guides_enabled = enabled
        self.update()

    def set_guide_division(self, division: int) -> None:
        self.guide_division = division if division in (4, 8, 16) else 4
        self.update()

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.fillRect(self.rect(), QColor(22, 24, 30))
        self._draw_background(p)
        self._draw_grid(p)
        if self.session.is_playing:
            self._draw_moving_guides(p)
            self._draw_moving_notes(p)
        else:
            self._draw_static_edit_grid(p)
        self._draw_judge_line(p)
        self._draw_header(p)

    def _draw_background(self, p: QPainter) -> None:
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(32, 36, 45))
        p.drawRoundedRect(self.rect().adjusted(2, 2, -2, -2), 10, 10)

    def _draw_header(self, p: QPainter) -> None:
        p.setPen(QColor(235, 235, 240))
        font = QFont()
        font.setBold(True)
        font.setPointSize(11)
        p.setFont(font)
        title = "お手本" if self.mode == "reference" else "実入力"
        p.drawText(12, 18, title)
        p.setPen(QColor(205, 205, 215))
        font.setBold(False)
        font.setPointSize(9)
        p.setFont(font)
        for i, lane in enumerate(self.model.lanes):
            x = self.margin_left + i * self.lane_width()
            p.drawText(QRectF(x, 6, self.lane_width(), 20), Qt.AlignCenter, lane)

    def _draw_grid(self, p: QPainter) -> None:
        lane_w = self.lane_width()
        full_h = self.grid_height()
        top = self.margin_top
        left = self.margin_left
        for i in range(len(self.model.lanes) + 1):
            x = left + i * lane_w
            pen = QPen(QColor(86, 92, 108), 1)
            if i in (0, len(self.model.lanes)):
                pen.setColor(QColor(120, 127, 145))
            p.setPen(pen)
            p.drawLine(int(x), int(top), int(x), int(top + full_h))
        total_steps = max(1, self.model.total_steps)
        row_h = full_h / total_steps
        for s in range(total_steps + 1):
            y = top + (total_steps - s) * row_h
            pen = QPen(QColor(64, 69, 81), 1)
            if s % self.model.steps_per_bar == 0:
                pen = QPen(QColor(127, 135, 156), 2)
            elif s % self.model.subdivision == 0:
                pen = QPen(QColor(92, 99, 116), 1)
            p.setPen(pen)
            if not self.session.is_playing:
                p.drawLine(int(left), int(y), int(left + lane_w * len(self.model.lanes)), int(y))

    def _draw_static_edit_grid(self, p: QPainter) -> None:
        if self.mode == "input":
            return
        lane_w = self.lane_width()
        row_h = self.grid_height() / max(1, self.model.total_steps)
        for note in self.model.notes:
            x = self.margin_left + self.model.lanes.index(note.lane) * lane_w + 6
            absolute_step = note.bar * self.model.steps_per_bar + note.step
            visual_step = (self.model.total_steps - 1) - absolute_step
            y = self.margin_top + visual_step * row_h + 3
            rect = QRectF(x, y, lane_w - 12, row_h - 6)
            p.setBrush(QColor(96, 184, 255))
            p.setPen(Qt.NoPen)
            p.drawRoundedRect(rect, 6, 6)

    def _draw_judge_line(self, p: QPainter) -> None:
        y = self.judge_line_y()
        p.setPen(QPen(QColor(255, 215, 120), 3))
        p.drawLine(self.margin_left, int(y), self.width() - self.margin_right, int(y))

    def _draw_moving_guides(self, p: QPainter) -> None:
        if not self.guides_enabled:
            return
        now = self.session.elapsed()
        beat = 60.0 / max(1, self.model.bpm)
        if self.guide_division == 4:
            step = beat
        elif self.guide_division == 8:
            step = beat / 2.0
        else:
            step = beat / 4.0
        if step <= 0:
            return
        start = now - (self.height() - self.margin_top - self.judge_line_y()) / self.scroll_speed - step * 2
        end = now + (self.judge_line_y() - self.margin_top) / self.scroll_speed + step * 2
        first_idx = int(math.floor(start / step))
        last_idx = int(math.ceil(end / step))
        left = self.margin_left
        right = self.width() - self.margin_right
        for idx in range(first_idx, last_idx + 1):
            t = idx * step
            y = self.judge_line_y() - (t - now) * self.scroll_speed
            if y < self.margin_top or y > self.height() - self.margin_bottom:
                continue
            is_bar = abs((t / beat) % self.model.beats_per_bar) < 1e-6 if beat > 0 else False
            if is_bar:
                pen = QPen(QColor(255, 255, 255, 220), 2)
            else:
                alpha = 150 if self.guide_division == 4 else (120 if self.guide_division == 8 else 90)
                pen = QPen(QColor(255, 255, 255, alpha), 1)
            p.setPen(pen)
            p.drawLine(int(left), int(y), int(right), int(y))

    def _draw_moving_notes(self, p: QPainter) -> None:
        lane_w = self.lane_width()
        now = self.session.elapsed()
        ref_notes = self.session.expected_hits_for_view() if self.mode == "reference" else []
        input_notes = self.session.recent_user_hits() if self.mode == "input" else []
        if self.mode == "reference":
            for n in ref_notes:
                self._draw_note_at_time(p, n.lane, n.time_sec, now, QColor(96, 184, 255), lane_w)
        else:
            evaluation = self.session.evaluate()
            matched = {}
            for exp, hit, err in evaluation:
                if hit is not None and err is not None:
                    matched[id(hit)] = err
            for h in input_notes:
                err = matched.get(id(h))
                color = QColor(140, 140, 140)
                if err is not None:
                    ms = err * 1000.0
                    if ms < -35:
                        color = QColor(85, 170, 255)
                    elif ms > 35:
                        color = QColor(255, 120, 110)
                    else:
                        color = QColor(120, 220, 140)
                self._draw_note_at_time(p, h.lane, h.time_sec - self.session.calibration_offset_sec, now, color, lane_w)

    def _draw_note_at_time(self, p: QPainter, lane: str, note_time: float, now: float, color: QColor, lane_w: float) -> None:
        y = self.judge_line_y() - (note_time - now) * self.scroll_speed
        if y < self.margin_top - 18 or y > self.height() - self.margin_bottom + 18:
            return
        lane_idx = self.model.lanes.index(lane)
        x = self.margin_left + lane_idx * lane_w + 10
        rect = QRectF(x, y - 10, lane_w - 20, 20)
        p.setBrush(color)
        p.setPen(Qt.NoPen)
        p.drawRoundedRect(rect, 7, 7)

    def mousePressEvent(self, event) -> None:
        if self.mode != "reference" or self.session.is_playing:
            return
        if event.button() != Qt.LeftButton:
            return
        x = event.x()
        y = event.y()
        if x < self.margin_left or x > self.width() - self.margin_right:
            return
        if y < self.margin_top or y > self.height() - self.margin_bottom:
            return
        lane_w = self.lane_width()
        lane_index = int((x - self.margin_left) // lane_w)
        if lane_index < 0 or lane_index >= len(self.model.lanes):
            return
        row_h = self.grid_height() / max(1, self.model.total_steps)
        visual_step = int((y - self.margin_top) // row_h)
        visual_step = max(0, min(self.model.total_steps - 1, visual_step))
        absolute_step = (self.model.total_steps - 1) - visual_step
        bar = absolute_step // self.model.steps_per_bar
        step = absolute_step % self.model.steps_per_bar
        self.note_clicked.emit(self.model.lanes[lane_index], bar, step)


class SummaryPanel(QFrame):
    def __init__(self, session: PracticeSession) -> None:
        super().__init__()
        self.session = session
        self.setFrameShape(QFrame.StyledPanel)
        self.setStyleSheet("QFrame { background: #20232B; border-radius: 8px; color: #EAEAF0; }")
        layout = QVBoxLayout(self)
        self.mean_label = QLabel("平均誤差: -")
        self.std_label = QLabel("ばらつき: -")
        self.count_label = QLabel("一致数: 0")
        for lbl in (self.mean_label, self.std_label, self.count_label):
            lbl.setStyleSheet("color: #EAEAF0; font-size: 14px;")
            layout.addWidget(lbl)
        self.session.session_updated.connect(self.refresh)
        self.session.session_reset.connect(self.refresh)

    def refresh(self) -> None:
        s = self.session.summary()
        if s["mean_ms"] is None:
            self.mean_label.setText("平均誤差: -")
            self.std_label.setText("ばらつき: -")
            self.count_label.setText("一致数: 0")
            return
        self.mean_label.setText(f"平均誤差: {s['mean_ms']:+.1f} ms")
        self.std_label.setText(f"ばらつき: {s['std_ms']:.1f} ms")
        self.count_label.setText(f"一致数: {int(s['count'])}")


class CalibrationController(QObject):
    calibration_computed = pyqtSignal(float, str)
    calibration_state_changed = pyqtSignal(str)

    def __init__(self) -> None:
        super().__init__()
        self.is_running = False
        self.hit_times: List[float] = []
        self.total_trials = 4
        self.interval = 0.5
        self.start_reference = 0.0
        self._result_emitted = False

    def start(self, bpm: int) -> None:
        self.is_running = True
        self.hit_times = []
        self.total_trials = 4
        self.interval = 60.0 / max(1, bpm)
        self.start_reference = time.perf_counter() + 2.4
        self._result_emitted = False
        self.calibration_state_changed.emit(f"キャリブレーション開始: BPM {bpm} の Kick 4つ打ちに合わせて叩いてください")

    def stop(self) -> None:
        self.is_running = False
        self.calibration_state_changed.emit("キャリブレーション停止")

    def expected_times(self) -> List[float]:
        if not self.is_running:
            return []
        return [self.start_reference + i * self.interval for i in range(self.total_trials)]

    def on_hit(self, lane: str, when: float) -> None:
        if not self.is_running or lane != "Kick":
            return
        if when < self.start_reference - 0.2:
            return
        if len(self.hit_times) >= self.total_trials:
            return
        self.hit_times.append(when)
        if len(self.hit_times) >= self.total_trials:
            self.finalize_if_ready()

    def finalize_if_ready(self) -> None:
        if not self.is_running or self._result_emitted:
            return
        expected = self.expected_times()
        if len(expected) != self.total_trials or len(self.hit_times) < self.total_trials:
            return
        diffs_sec = [h - e for h, e in zip(self.hit_times[: self.total_trials], expected)]
        median_sec = statistics.median(diffs_sec)
        mean_sec = statistics.mean(diffs_sec)
        text = f"算出補正値: {median_sec * 1000:+.1f} ms\n平均ずれ: {mean_sec * 1000:+.1f} ms\n対象打数: {len(diffs_sec)}"
        self._result_emitted = True
        self.is_running = False
        self.calibration_state_changed.emit(f"キャリブレーション完了: {median_sec * 1000:+.1f} ms")
        self.calibration_computed.emit(median_sec, text)

    def maybe_timeout_finalize(self) -> None:
        if not self.is_running or self._result_emitted:
            return
        expected = self.expected_times()
        if not expected:
            return
        if time.perf_counter() < expected[-1] + max(0.8, self.interval * 0.8):
            return
        if not self.hit_times:
            self.is_running = False
            self.calibration_state_changed.emit("キャリブレーション完了: 入力がありませんでした")
            self.calibration_computed.emit(0.0, "入力が取得できなかったため、補正値は算出できませんでした。")
            self._result_emitted = True
            return
        count = min(len(self.hit_times), len(expected))
        diffs_sec = [h - e for h, e in zip(self.hit_times[:count], expected[:count])]
        median_sec = statistics.median(diffs_sec)
        mean_sec = statistics.mean(diffs_sec)
        text = f"算出補正値: {median_sec * 1000:+.1f} ms\n平均ずれ: {mean_sec * 1000:+.1f} ms\n対象打数: {count} / {self.total_trials}"
        self._result_emitted = True
        self.is_running = False
        self.calibration_state_changed.emit(f"キャリブレーション完了: {median_sec * 1000:+.1f} ms")
        self.calibration_computed.emit(median_sec, text)


class CalibrationView(QFrame):
    def __init__(self, controller: CalibrationController) -> None:
        super().__init__()
        self.controller = controller
        self.setMinimumHeight(180)
        self.setStyleSheet("QFrame { background: #1D2027; border-radius: 8px; }")
        self.scroll_speed = 120.0
        timer = QTimer(self)
        timer.timeout.connect(self.update)
        timer.start(16)

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.fillRect(self.rect(), QColor(29, 32, 39))
        w = self.width()
        h = self.height()
        judge_y = h * 0.84
        p.setPen(QPen(QColor(255, 215, 120), 3))
        p.drawLine(20, int(judge_y), w - 20, int(judge_y))
        p.setPen(QColor(230, 232, 240))
        p.drawText(14, 18, "キャリブレーションビュー (Kick 4つ打ち)")
        if not self.controller.is_running:
            p.setPen(QColor(170, 174, 186))
            p.drawText(14, 42, "待機中")
            return
        now = time.perf_counter()
        p.setPen(QPen(QColor(80, 88, 102), 1))
        for x in (w * 0.30, w * 0.50, w * 0.70):
            p.drawLine(int(x), 24, int(x), h - 12)
        p.setBrush(QColor(96, 184, 255))
        p.setPen(Qt.NoPen)
        for t in self.controller.expected_times():
            y = judge_y - (t - now) * self.scroll_speed
            if 18 <= y <= h - 8:
                p.drawRoundedRect(QRectF(w / 2 - 54, y - 10, 108, 20), 7, 7)
        p.setPen(QColor(170, 174, 186))
        p.drawText(14, 62, "流れてくるKickが判定線に重なる瞬間に叩いてください")


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Drum Pattern Trainer Prototype")
        self.resize(1320, 900)

        self.model = PatternModel()
        self.session = PracticeSession(self.model)
        self.midi = MidiInputManager()
        self.calibration = CalibrationController()
        self.audio = AudioEngine()
        self._pending_calibration_sec: Optional[float] = None
        self.metronome_enabled = True
        self.reference_audio_enabled = True
        self.input_audio_enabled = True
        self._last_audio_elapsed = 0.0
        self._last_beat_index = -1
        self._last_calibration_index = -1

        self._build_ui()
        self._connect_signals()

        self.update_timer = QTimer(self)
        self.update_timer.timeout.connect(self._tick)
        self.update_timer.start(16)

        self._sync_offset_widgets(self.session.calibration_offset_sec)
        self.model.pattern_changed.connect(self._rebuild_lane_order_list)
        self._on_toggle_guides(True)
        self._on_change_guide_division(self.guide_division_combo.currentText())
        self.midi.refresh_ports()

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        control = QFrame()
        control.setStyleSheet("QFrame { background: #20232B; border-radius: 8px; } QLabel { color: #EAEAF0; }")
        control_layout = QGridLayout(control)

        self.bpm_spin = QSpinBox()
        self.bpm_spin.setRange(40, 240)
        self.bpm_spin.setValue(120)
        self.bar_spin = QSpinBox()
        self.bar_spin.setRange(1, 8)
        self.bar_spin.setValue(2)
        self.play_button = QPushButton("再生")
        self.stop_button = QPushButton("停止")
        self.calib_button = QPushButton("キャリブレーション開始")
        self.sample_zip_button = QPushButton("音源zip読込")
        self.sample_folder_button = QPushButton("音源フォルダ読込")
        self.metro_button = QPushButton("メトロノーム ON")
        self.metro_button.setCheckable(True)
        self.metro_button.setChecked(True)
        self.ref_audio_button = QPushButton("お手本音 ON")
        self.ref_audio_button.setCheckable(True)
        self.ref_audio_button.setChecked(True)
        self.input_audio_button = QPushButton("入力音 ON")
        self.input_audio_button.setCheckable(True)
        self.input_audio_button.setChecked(True)
        self.guide_toggle = QPushButton("白線 ON")
        self.guide_toggle.setCheckable(True)
        self.guide_toggle.setChecked(True)
        self.guide_division_combo = QComboBox()
        self.guide_division_combo.addItems(["4分", "8分", "16分"])
        self.refresh_midi_button = QPushButton("MIDI更新")
        self.midi_combo = QComboBox()
        self.offset_label = QLabel("補正値: +0.0 ms")
        self.offset_spin = QDoubleSpinBox()
        self.offset_spin.setRange(-1000.0, 1000.0)
        self.offset_spin.setDecimals(1)
        self.offset_spin.setSingleStep(1.0)
        self.offset_spin.setSuffix(" ms")
        self.offset_spin.setValue(0.0)
        self.status_label = QLabel("A/S/D/F/G でも入力可能")
        self.lane_order_list = QListWidget()
        self.lane_order_list.setFixedHeight(150)
        self.lane_order_list.setDragDropMode(QListWidget.InternalMove)
        self.lane_order_list.setDefaultDropAction(Qt.MoveAction)
        self.lane_order_list.setSelectionMode(QListWidget.SingleSelection)
        self.apply_lane_order_button = QPushButton("レーン順を適用")
        self.reset_lane_order_button = QPushButton("初期配置に戻す")
        self._rebuild_lane_order_list()

        row = 0
        control_layout.addWidget(QLabel("BPM"), row, 0)
        control_layout.addWidget(self.bpm_spin, row, 1)
        control_layout.addWidget(QLabel("小節数"), row, 2)
        control_layout.addWidget(self.bar_spin, row, 3)
        control_layout.addWidget(QLabel("MIDI"), row, 4)
        control_layout.addWidget(self.midi_combo, row, 5)
        control_layout.addWidget(self.refresh_midi_button, row, 6)
        control_layout.addWidget(self.sample_zip_button, row, 7)
        control_layout.addWidget(self.sample_folder_button, row, 8)
        control_layout.addWidget(self.play_button, row, 9)
        control_layout.addWidget(self.stop_button, row, 10)
        control_layout.addWidget(self.calib_button, row, 11)
        control_layout.addWidget(self.offset_label, row, 12)
        control_layout.addWidget(QLabel("手動補正"), 1, 0)
        control_layout.addWidget(self.offset_spin, 1, 1)
        control_layout.addWidget(self.metro_button, 1, 2)
        control_layout.addWidget(self.ref_audio_button, 1, 3)
        control_layout.addWidget(self.input_audio_button, 1, 4)
        control_layout.addWidget(self.guide_toggle, 1, 5)
        control_layout.addWidget(QLabel("白線間隔"), 1, 6)
        control_layout.addWidget(self.guide_division_combo, 1, 7)
        control_layout.addWidget(self.status_label, 1, 8, 1, 5)

        root.addWidget(control)

        lane_config = QFrame()
        lane_config.setStyleSheet("QFrame { background: #20232B; border-radius: 8px; } QLabel { color: #EAEAF0; }")
        lane_layout = QHBoxLayout(lane_config)
        lane_layout.addWidget(QLabel("レーン順 (ドラッグで並べ替え)"))
        lane_layout.addWidget(self.lane_order_list, 1)
        lane_buttons = QVBoxLayout()
        lane_buttons.addWidget(self.apply_lane_order_button)
        lane_buttons.addWidget(self.reset_lane_order_button)
        lane_buttons.addStretch(1)
        lane_layout.addLayout(lane_buttons)
        root.addWidget(lane_config)

        middle = QHBoxLayout()
        middle.setSpacing(8)
        self.reference_panel = LanePanel(self.model, self.session, mode="reference")
        self.input_panel = LanePanel(self.model, self.session, mode="input")
        middle.addWidget(self.reference_panel, 1)
        middle.addWidget(self.input_panel, 1)

        right_column = QVBoxLayout()
        self.summary_panel = SummaryPanel(self.session)
        self.calibration_view = CalibrationView(self.calibration)
        right_column.addWidget(self.summary_panel)
        right_column.addWidget(self.calibration_view)
        right_column.addStretch(1)

        wrapper = QHBoxLayout()
        wrapper.addLayout(middle, 4)
        wrapper.addLayout(right_column, 1)
        root.addLayout(wrapper, 1)

    def _connect_signals(self) -> None:
        self.bpm_spin.valueChanged.connect(self.model.set_bpm)
        self.bar_spin.valueChanged.connect(self.model.set_bars)
        self.reference_panel.note_clicked.connect(self._on_reference_note_clicked)
        self.play_button.clicked.connect(self._on_play)
        self.stop_button.clicked.connect(self._on_stop)
        self.calib_button.clicked.connect(self._on_calibration_toggle)
        self.sample_zip_button.clicked.connect(self._on_load_sample_zip)
        self.sample_folder_button.clicked.connect(self._on_load_sample_folder)
        self.apply_lane_order_button.clicked.connect(self._on_apply_lane_order)
        self.reset_lane_order_button.clicked.connect(self._on_reset_lane_order)
        self.offset_spin.valueChanged.connect(self._on_manual_offset_changed)
        self.metro_button.toggled.connect(self._on_toggle_metronome)
        self.ref_audio_button.toggled.connect(self._on_toggle_reference_audio)
        self.input_audio_button.toggled.connect(self._on_toggle_input_audio)
        self.guide_toggle.toggled.connect(self._on_toggle_guides)
        self.guide_division_combo.currentTextChanged.connect(self._on_change_guide_division)
        self.refresh_midi_button.clicked.connect(self.midi.refresh_ports)
        self.midi.ports_changed.connect(self._update_midi_ports)
        self.midi_combo.currentTextChanged.connect(self._on_midi_selected)
        self.midi.hit_received.connect(self._on_hit_received)
        self.calibration.calibration_computed.connect(self._on_calibration_computed)
        self.calibration.calibration_state_changed.connect(self.status_label.setText)

    def _on_reference_note_clicked(self, lane: str, bar: int, step: int) -> None:
        was_present = self.model.has_note(lane, bar, step)
        self.model.toggle_note(lane, bar, step)
        # ノート追加時に、そのレーンの音を鳴らす
        if not was_present:
            self.audio.play(lane)

    def _rebuild_lane_order_list(self) -> None:
        if not hasattr(self, "lane_order_list"):
            return
        self.lane_order_list.clear()
        for lane in self.model.lanes:
            self.lane_order_list.addItem(QListWidgetItem(lane))

    def _current_lane_order_from_ui(self) -> List[str]:
        return [self.lane_order_list.item(i).text() for i in range(self.lane_order_list.count())]

    def _on_apply_lane_order(self) -> None:
        lanes = self._current_lane_order_from_ui()
        if sorted(lanes) != sorted(BASE_LANES):
            QMessageBox.warning(self, "レーン順エラー", "レーン構成が不正です。")
            self._rebuild_lane_order_list()
            return
        self.model.set_lane_order(lanes)
        self.status_label.setText("レーン順を適用しました")

    def _on_reset_lane_order(self) -> None:
        self.model.set_lane_order(list(BASE_LANES))
        self._rebuild_lane_order_list()
        self.status_label.setText("レーン順を初期配置に戻しました")

    def _update_midi_ports(self, ports: List[str]) -> None:
        current = self.midi_combo.currentText()
        self.midi_combo.blockSignals(True)
        self.midi_combo.clear()
        self.midi_combo.addItem("Keyboard Fallback")
        self.midi_combo.addItems(ports)
        index = self.midi_combo.findText(current)
        self.midi_combo.setCurrentIndex(index if index >= 0 else 0)
        self.midi_combo.blockSignals(False)
        self._on_midi_selected(self.midi_combo.currentText())

    def _on_midi_selected(self, text: str) -> None:
        ok = self.midi.open_port(text)
        if not ok:
            self.status_label.setText("MIDIポートを開けませんでした。Keyboard Fallback を使います。")
        elif text == "Keyboard Fallback":
            self.status_label.setText("A/S/D/F/G で Kick/Snare/Hi-Hat/Tom/Crash を入力できます。")
        else:
            self.status_label.setText(f"MIDI接続: {text}")

    def _on_play(self) -> None:
        if not self.model.notes:
            QMessageBox.information(self, "情報", "左側のお手本UIにノートを配置してください。")
            return
        self.session.start()
        self._last_audio_elapsed = 0.0
        self._last_beat_index = -1
        self._last_calibration_index = -1
        self.status_label.setText("再生中")

    def _on_stop(self) -> None:
        self.session.stop()
        self.status_label.setText("停止")

    def _on_calibration_toggle(self) -> None:
        if self.calibration.is_running:
            self.calibration.stop()
            self.calib_button.setText("キャリブレーション開始")
            return
        self.calibration.start(self.model.bpm)
        self.calib_button.setText("キャリブレーション停止")
        self._pending_calibration_sec = None
        self._last_calibration_index = -1

    def _sync_offset_widgets(self, offset_sec: float) -> None:
        self.session.calibration_offset_sec = offset_sec
        self.offset_label.setText(f"補正値: {offset_sec * 1000:+.1f} ms")
        blocked = self.offset_spin.blockSignals(True)
        self.offset_spin.setValue(offset_sec * 1000.0)
        self.offset_spin.blockSignals(blocked)

    def _apply_calibration(self, offset_sec: float) -> None:
        self._sync_offset_widgets(offset_sec)
        self.calib_button.setText("キャリブレーション開始")

    def _on_manual_offset_changed(self, value_ms: float) -> None:
        self._sync_offset_widgets(value_ms / 1000.0)
        self.status_label.setText(f"手動補正を設定: {value_ms:+.1f} ms")

    def _on_calibration_computed(self, offset_sec: float, message: str) -> None:
        self._pending_calibration_sec = offset_sec
        self.calib_button.setText("キャリブレーション開始")
        reply = QMessageBox.question(
            self,
            "キャリブレーション結果",
            message + "\n\nこの補正値を適用しますか？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes,
        )
        if reply == QMessageBox.Yes:
            self._apply_calibration(offset_sec)
            self.status_label.setText(f"補正値を適用しました: {offset_sec * 1000:+.1f} ms")
        else:
            self.status_label.setText(f"補正値は適用しませんでした: {offset_sec * 1000:+.1f} ms")

    def _on_hit_received(self, lane: str, when: float, velocity: int, source: str) -> None:
        if self.calibration.is_running:
            self.calibration.on_hit(lane, when)
        self.session.register_hit(lane, when, velocity, source)
        if self.input_audio_enabled:
            self.audio.play(lane)

    def _tick(self) -> None:
        if self.session.is_playing:
            self._process_audio()
            self.session.session_updated.emit()
        self.calibration_view.update()

    def _process_audio(self) -> None:
        now = self.session.elapsed()
        last = self._last_audio_elapsed
        if now < last:
            last = 0.0
            self._last_beat_index = -1
        loop_duration = self.model.loop_duration
        if loop_duration > 0 and self.reference_audio_enabled:
            start_loop = max(0, int(last // loop_duration) - 1)
            end_loop = int(now // loop_duration) + 1
            for n in self.session.reference_hits:
                for loop_idx in range(start_loop, end_loop + 1):
                    t = n.time_sec + loop_idx * loop_duration
                    if last < t <= now:
                        self.audio.play(n.lane)
        if self.metronome_enabled:
            beat_dur = 60.0 / max(1, self.model.bpm)
            cur_beat = int(now // beat_dur)
            while self._last_beat_index < cur_beat:
                self._last_beat_index += 1
                beat_in_bar = self._last_beat_index % self.model.beats_per_bar
                self.audio.play("metronome_major" if beat_in_bar == 0 else "metronome_minor")
        if self.calibration.is_running and self.reference_audio_enabled:
            rel_now = time.perf_counter()
            for idx, t in enumerate(self.calibration.expected_times()):
                if idx <= self._last_calibration_index:
                    continue
                if t <= rel_now:
                    self.audio.play("Kick")
                    self._last_calibration_index = idx
            self.calibration.finalize_if_ready()
            self.calibration.maybe_timeout_finalize()
        self._last_audio_elapsed = now

    def _show_sample_load_result(self, path: str, ok: bool, loaded: List[str], missing: List[str]) -> None:
        if not ok:
            QMessageBox.warning(self, "読込失敗", "\n".join(missing) if missing else "読み込めませんでした。")
            return
        msg = ["音源を読み込みました。", ""]
        if loaded:
            msg.append("読めた音:")
            msg.extend(loaded[:12])
        if missing:
            msg.append("")
            msg.append("見つからなかった音(内蔵音を使用):")
            msg.append(", ".join(missing))
        self.status_label.setText(f"音源読込: {Path(path).name}")
        QMessageBox.information(self, "音源読込", "\n".join(msg))

    def _on_load_sample_zip(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "音源zipを選択", "", "Zip Files (*.zip);;All Files (*)")
        if not path:
            return
        ok, loaded, missing = self.audio.load_sample_source(path)
        self._show_sample_load_result(path, ok, loaded, missing)

    def _on_load_sample_folder(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "音源フォルダを選択")
        if not path:
            return
        ok, loaded, missing = self.audio.load_sample_source(path)
        self._show_sample_load_result(path, ok, loaded, missing)

    def _on_toggle_metronome(self, checked: bool) -> None:
        self.metronome_enabled = checked
        self.metro_button.setText(f"メトロノーム {'ON' if checked else 'OFF'}")

    def _on_toggle_reference_audio(self, checked: bool) -> None:
        self.reference_audio_enabled = checked
        self.ref_audio_button.setText(f"お手本音 {'ON' if checked else 'OFF'}")

    def _on_toggle_input_audio(self, checked: bool) -> None:
        self.input_audio_enabled = checked
        self.input_audio_button.setText(f"入力音 {'ON' if checked else 'OFF'}")

    def _on_toggle_guides(self, checked: bool) -> None:
        self.guide_toggle.setText(f"白線 {'ON' if checked else 'OFF'}")
        self.reference_panel.set_guides_enabled(checked)
        self.input_panel.set_guides_enabled(checked)

    def _on_change_guide_division(self, text: str) -> None:
        mapping = {"4分": 4, "8分": 8, "16分": 16}
        division = mapping.get(text, 4)
        self.reference_panel.set_guide_division(division)
        self.input_panel.set_guide_division(division)

    def keyPressEvent(self, event) -> None:
        lane = KEYBOARD_FALLBACK_MAP.get(event.key())
        if lane is not None:
            self.midi.emit_keyboard_hit(lane)
            event.accept()
            return
        super().keyPressEvent(event)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyleSheet(
        """
        QWidget { background: #17191F; color: #EAEAF0; }
        QPushButton { background: #2A2F3A; border: 1px solid #4B5363; padding: 6px 10px; border-radius: 6px; }
        QPushButton:hover { background: #333947; }
        QSpinBox, QComboBox, QDoubleSpinBox { background: #0F1116; border: 1px solid #4B5363; padding: 4px 6px; border-radius: 6px; }
        """
    )
    window = MainWindow()
    window.show()
    sys.exit(app.exec_())
