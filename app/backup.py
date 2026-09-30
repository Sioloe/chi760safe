"""Durable, append-only TXT output for validated CHI samples (standard library)."""
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import json
import math
import os
import uuid

TECHNIQUES = {4: ('CA', '电流(mA)', 'Current(A)', 1000.0),
              11: ('i-t', '电流(mA)', 'Current(A)', 1000.0),
              15: ('CP', '电位(V)', 'Potential(V)', 1.0)}


@dataclass(frozen=True)
class Snapshot:
    key: str
    technique: int
    points: tuple[tuple[float, float], ...]
    origin: str
    complete: bool = False


def validate_points(points):
    previous = -1.0
    for t, y in points:
        if not math.isfinite(t) or not math.isfinite(y) or t < 0 or t <= previous:
            raise ValueError('时间列不递增或含无效值，本次快照未写入；请保留原始文件检查。')
        previous = t


class BackupWriter:
    def __init__(self, root):
        self.root = Path(root)
        self.directory = None
        self.key = None
        self.technique = None
        self.points = []
        self.flushed = 0
        self.main = self.raw = None
        self.last_saved = ''
        self.events = []
        self.failed = False

    def _start(self, snapshot):
        self.close()
        self.root.mkdir(parents=True, exist_ok=True)
        name = datetime.now().strftime('CHI_%Y%m%d_%H%M%S_') + uuid.uuid4().hex[:8]
        self.directory = self.root / name
        self.directory.mkdir()
        short, chinese, raw, scale = TECHNIQUES[snapshot.technique]
        self.key, self.technique = snapshot.key, snapshot.technique
        self.points, self.flushed, self.events = [], 0, []
        metadata = {'program': '为i发电 · CHI 备份', 'version': '0.1-preview',
                    'technique': short, 'origin': snapshot.origin,
                    'created': datetime.now().isoformat(), 'source_key': snapshot.key,
                    'data_columns': ['Index', 'Time(s)', raw],
                    'note': 'TXT仅包含实际记录的时间和测量值；未推算另一电学量或电荷。'}
        with (self.directory / '说明.json').open('x', encoding='utf-8') as f:
            json.dump(metadata, f, ensure_ascii=False, indent=2)
            f.flush(); os.fsync(f.fileno())
        self.main = (self.directory / 'data.txt').open('x', encoding='utf-8-sig', newline='')
        self.raw = (self.directory / 'raw_SI.txt').open('x', encoding='utf-8-sig', newline='')
        self.main.write(f'序号\t时间(s)\t{chinese}\r\n')
        self.raw.write(f'Index\tTime(s)\t{raw}\r\n')
        self.flush()

    def accept(self, snapshot):
        if self.failed:
            raise OSError('此前写盘失败，必须停止并重新启动备份。')
        if snapshot.technique not in TECHNIQUES:
            raise ValueError('当前仅支持单通道 CA、i–t、CP。')
        validate_points(snapshot.points)
        if not snapshot.points:
            return
        if self.key != snapshot.key or self.technique != snapshot.technique:
            self._start(snapshot)
        # Already saved points are immutable. A rewritten series becomes a new file.
        old = dict(self.points)
        last_t = self.points[-1][0] if self.points else -1.0
        rewritten = any(t in old and old[t] != y for t, y in snapshot.points if t <= last_t)
        compacted = len(snapshot.points) < len(self.points) and snapshot.points[-1][0] >= last_t
        if (snapshot.points[-1][0] < last_t or (rewritten and not compacted)):
            self._start(snapshot)
            last_t = -1.0
        new = [(t, y) for t, y in snapshot.points if t > last_t]
        self.points.extend(new)
        if compacted and 'CHI缓冲区已缩减；保留已备份点，新点按原时间追加。' not in self.events:
            self.events.append('CHI缓冲区已缩减；保留已备份点，新点按原时间追加。')
            (self.directory / '缓冲区提示.txt').write_text('\n'.join(self.events), encoding='utf-8-sig')

    def flush(self):
        if self.main is None:
            return
        scale = TECHNIQUES[self.technique][3]
        pending = self.points[self.flushed:]
        # Checkpoints are advanced only after both files are flushed to the OS.
        # On a write error the controller stops, preserving all files for recovery.
        try:
            for index, (t, y) in enumerate(pending, self.flushed + 1):
                self.main.write(f'{index}\t{t:.9g}\t{y * scale:.9g}\r\n')
                self.raw.write(f'{index}\t{t:.9g}\t{y:.9g}\r\n')
            for f in (self.main, self.raw):
                f.flush(); os.fsync(f.fileno())
        except BaseException:
            self.failed = True
            raise
        self.flushed = len(self.points)
        self.last_saved = datetime.now().strftime('%H:%M:%S')

    def close(self):
        if self.main is not None:
            try:
                if not self.failed:
                    self.flush()
            finally:
                for f in (self.main, self.raw):
                    f.close()
                self.main = self.raw = None


def read_recovery(data: bytes, key='recovery'):
    """CHI760E 15.3 retrieve v105. Only verified, primary single-channel layouts."""
    import struct
    if len(data) < 676:
        raise ValueError('恢复文件头尚未写完（少于676字节）。')
    integer = lambda offset: struct.unpack_from('<i', data, offset)[0]
    if integer(0) != 105:
        raise ValueError('未支持的恢复文件版本；没有猜测文件格式。')
    year, month, day, hour, minute, second = struct.unpack_from('<6i', data, 4)
    try:
        datetime(year, month, day, hour, minute, second)
    except ValueError:
        raise ValueError('恢复文件日期字段无效。')
    tech = integer(28)
    if tech not in TECHNIQUES:
        raise ValueError(f'恢复文件技术编号 {tech} 尚未支持，当前仅支持 CA、i–t、CP。')
    flags = {4: (52, 64, 68, 80, 96), 11: (56, 80, 96), 15: (80, 96)}[tech]
    if any(integer(p) != 0 for p in flags):
        raise ValueError('该文件启用了附加通道；本版只解析主通道单曲线，未猜测列结构。')
    end = 676 + ((len(data) - 676) // 8) * 8
    points = tuple(struct.iter_unpack('<ff', data[676:end]))
    validate_points(points)
    return Snapshot(key, tech, points, 'CHI恢复文件（完整记录，末尾不足8字节已忽略）', True)
