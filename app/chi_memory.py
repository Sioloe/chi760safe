"""Read-only adapter for the exact, user-provided CHI760E 15.3 executable.

No hooks, process writes, injected code, instrument commands or process suspension.
Unsupported builds fail closed. Offsets are from local format inspection; real
instrument validation is still required for this preview.
"""
import ctypes as C
from ctypes import wintypes as W
from pathlib import Path
import hashlib
import struct
import time
from backup import Snapshot, TECHNIQUES, validate_points

SUPPORTED_SHA256 = 'cccf827f205e7f27b548252defd53b9be6b2cb8c646a9c4742d1472d1633f347'
IMAGE_BASE = 0x400000
DOC_VTABLE = 0xBD9CAC
FILE_VTABLE = 0xCC2334
# DBF418 is the stop-request flag. DBF44C is set at acquisition entry and
# cleared after the acquisition call returns (verified in the supplied build).
RUN_FLAG = 0xDBF44C
DATA_LIMIT = 0xD394D0


class Waiting(Exception):
    pass


class Unsupported(Exception):
    pass


class PROCESSENTRY32W(C.Structure):
    _fields_ = [('dwSize', W.DWORD), ('cntUsage', W.DWORD), ('th32ProcessID', W.DWORD),
                ('th32DefaultHeapID', C.c_size_t), ('th32ModuleID', W.DWORD),
                ('cntThreads', W.DWORD), ('th32ParentProcessID', W.DWORD),
                ('pcPriClassBase', W.LONG), ('dwFlags', W.DWORD), ('szExeFile', W.WCHAR * 260)]


class MODULEENTRY32W(C.Structure):
    _fields_ = [('dwSize', W.DWORD), ('th32ModuleID', W.DWORD), ('th32ProcessID', W.DWORD),
                ('GlblcntUsage', W.DWORD), ('ProccntUsage', W.DWORD),
                ('modBaseAddr', C.c_void_p), ('modBaseSize', W.DWORD),
                ('hModule', W.HMODULE), ('szModule', W.WCHAR * 256), ('szExePath', W.WCHAR * 260)]


class MEMORY_BASIC_INFORMATION(C.Structure):
    _fields_ = [('BaseAddress', C.c_void_p), ('AllocationBase', C.c_void_p),
                ('AllocationProtect', W.DWORD), ('PartitionId', W.WORD),
                ('RegionSize', C.c_size_t), ('State', W.DWORD), ('Protect', W.DWORD),
                ('Type', W.DWORD)]


def kernel():
    k = C.WinDLL('kernel32', use_last_error=True)
    signatures = {
        'CreateToolhelp32Snapshot': ([W.DWORD, W.DWORD], W.HANDLE),
        'Process32FirstW': ([W.HANDLE, C.POINTER(PROCESSENTRY32W)], W.BOOL),
        'Process32NextW': ([W.HANDLE, C.POINTER(PROCESSENTRY32W)], W.BOOL),
        'Module32FirstW': ([W.HANDLE, C.POINTER(MODULEENTRY32W)], W.BOOL),
        'OpenProcess': ([W.DWORD, W.BOOL, W.DWORD], W.HANDLE),
        'CloseHandle': ([W.HANDLE], W.BOOL),
        'ReadProcessMemory': ([W.HANDLE, C.c_void_p, C.c_void_p, C.c_size_t, C.POINTER(C.c_size_t)], W.BOOL),
        'VirtualQueryEx': ([W.HANDLE, C.c_void_p, C.POINTER(MEMORY_BASIC_INFORMATION), C.c_size_t], C.c_size_t),
        'QueryFullProcessImageNameW': ([W.HANDLE, W.DWORD, W.LPWSTR, C.POINTER(W.DWORD)], W.BOOL),
        'GetExitCodeProcess': ([W.HANDLE, C.POINTER(W.DWORD)], W.BOOL),
    }
    for name, (args, result) in signatures.items():
        f = getattr(k, name); f.argtypes = args; f.restype = result
    return k


def list_chi_processes():
    k = kernel()
    handle = k.CreateToolhelp32Snapshot(2, 0)
    if handle in (None, C.c_void_p(-1).value):
        raise C.WinError(C.get_last_error())
    found = []
    try:
        entry = PROCESSENTRY32W(); entry.dwSize = C.sizeof(entry)
        ok = k.Process32FirstW(handle, C.byref(entry))
        while ok:
            if entry.szExeFile.lower() == 'chi760e.exe':
                found.append((int(entry.th32ProcessID), entry.szExeFile))
            ok = k.Process32NextW(handle, C.byref(entry))
    finally:
        k.CloseHandle(handle)
    return found


class Memory:
    def __init__(self, pid):
        self.pid, self.k = pid, kernel()
        self.handle = self.k.OpenProcess(0x0400 | 0x0010, False, pid)  # query + read only
        if not self.handle:
            raise OSError('无法只读访问 CHI；请让两个程序使用相同账户和权限级别。')
        try:
            path = C.create_unicode_buffer(32768); size = W.DWORD(len(path))
            if not self.k.QueryFullProcessImageNameW(self.handle, 0, path, C.byref(size)):
                raise C.WinError(C.get_last_error())
            self.path = Path(path.value)
            if hashlib.sha256(self.path.read_bytes()).hexdigest() != SUPPORTED_SHA256:
                raise Unsupported('此 CHI 程序与所提供的 15.3 版本不同，已停止读取以避免错列。')
            # PSAPI supports a 64-bit companion reading the 32-bit CHI build.
            psapi = C.WinDLL('psapi', use_last_error=True)
            enum = psapi.EnumProcessModulesEx
            enum.argtypes = [W.HANDLE, C.POINTER(W.HMODULE), W.DWORD, C.POINTER(W.DWORD), W.DWORD]
            enum.restype = W.BOOL
            get_name = psapi.GetModuleFileNameExW
            get_name.argtypes = [W.HANDLE, W.HMODULE, W.LPWSTR, W.DWORD]; get_name.restype = W.DWORD
            modules = (W.HMODULE * 1024)(); needed = W.DWORD()
            if not enum(self.handle, modules, C.sizeof(modules), C.byref(needed), 3):
                raise C.WinError(C.get_last_error())
            self.base = None
            for module in modules[:min(len(modules), needed.value // C.sizeof(W.HMODULE))]:
                name = C.create_unicode_buffer(32768)
                if get_name(self.handle, module, name, len(name)) and Path(name.value).resolve() == self.path.resolve():
                    self.base = int(module); break
            if self.base is None:
                raise Unsupported('无法确认 CHI 主模块，已停止读取。')
        except BaseException:
            self.close(); raise

    def absolute(self, value):
        return value - IMAGE_BASE + self.base

    def close(self):
        if self.handle:
            self.k.CloseHandle(self.handle); self.handle = None

    def read(self, address, length):
        if length < 0 or length > 16 * 1024 * 1024 or address < 0x10000:
            raise Waiting('缓冲区地址或长度尚未就绪。')
        if not length:
            return b''
        data = C.create_string_buffer(length); got = C.c_size_t()
        if not self.k.ReadProcessMemory(self.handle, address, data, length, C.byref(got)) or got.value != length:
            raise Waiting('CHI 正在更新或释放数据缓冲区，等待下一次快照。')
        return data.raw

    def u32(self, address):
        return struct.unpack('<I', self.read(address, 4))[0]

    def alive(self):
        code = W.DWORD()
        return bool(self.k.GetExitCodeProcess(self.handle, C.byref(code))) and code.value == 259

    def find(self, needle, max_bytes=256 * 1024 * 1024):
        """Search only this selected program's readable private allocations."""
        address, total = 0x10000, 0
        while address < 0x80000000 and total < max_bytes:
            info = MEMORY_BASIC_INFORMATION()
            if not self.k.VirtualQueryEx(self.handle, address, C.byref(info), C.sizeof(info)):
                break
            start, end = int(info.BaseAddress or address), int(info.BaseAddress or address) + info.RegionSize
            if end <= address:
                break
            if info.State == 0x1000 and info.Type == 0x20000 and (info.Protect & 0xFF) in (0x04, 0x08, 0x40, 0x80) and not info.Protect & 0x100:
                cursor = start
                while cursor < end and total < max_bytes:
                    length = min(1024 * 1024, end - cursor, max_bytes - total)
                    try:
                        data = self.read(cursor, length)
                    except Waiting:
                        cursor += length; total += length; continue
                    index = data.find(needle)
                    while index >= 0:
                        if (cursor + index) % 4 == 0:
                            yield cursor + index
                        index = data.find(needle, index + 1)
                    cursor += max(1, length - 3); total += length
            address = end


class CHIReader:
    def __init__(self, memory):
        self.memory = memory
        self.live_address = self.doc_address = None
        self.was_running = False
        self.epoch = 0
        self.last_scan = 0.0
        self.run_key = ''
        self.last_live = None
        self.finished = False
        self.finish_since = None

    def _live_header(self, address):
        m = self.memory
        if m.u32(address + 0xCC0) != m.absolute(FILE_VTABLE):
            raise Waiting('等待当前测试缓冲区。')
        # This build creates the acquisition object on the CEcDoc run stack.
        # Verify its parent document too, so an unrelated CFile cannot match.
        parent = m.u32(address - 0x3120)
        if (m.u32(parent) != m.absolute(DOC_VTABLE) or
                m.u32(parent + 0x8D520C) != 1):
            raise Waiting('等待当前实验的数据对象。')
        block = m.read(address + 0xF00, 0xAC0)
        get = lambda o: struct.unpack_from('<I', block, o - 0xF00)[0]
        tech, count, x, y = get(0x12F4), get(0x1344), get(0x1984), get(0x1988)
        cap = m.u32(m.absolute(DATA_LIMIT))
        if not 0 < cap <= 2_000_000 or count > cap or x < 0x10000 or y < 0x10000 or x == y:
            raise Waiting('采样点数或缓冲区尚未就绪。')
        if tech not in TECHNIQUES:
            raise Unsupported(f'当前技术编号 {tech} 暂未支持，本版支持 CA、i–t、CP。')
        if any(get(o) for o in (0x100C, 0x101C, 0x1020, 0x1028, 0x102C, 0x19BC)):
            raise Unsupported('检测到辅助/第二通道，本预览版只支持主通道单曲线。')
        return tech, count, x, y

    def _doc_header(self, address):
        m = self.memory
        if m.u32(address) != m.absolute(DOC_VTABLE):
            raise Waiting('等待实验文档。')
        tech = m.u32(address + 0x8D3920)
        count = m.u32(address + 0x8D397C)
        x, y = m.u32(address + 0x8D87D4), m.u32(address + 0x8D87DC)
        cap = m.u32(m.absolute(DATA_LIMIT))
        if tech not in TECHNIQUES or not 0 < count <= cap <= 2_000_000 or min(x, y) < 0x10000 or x == y:
            raise Waiting('没有已完成的受支持数据。')
        if any(m.u32(address + o) for o in (0x8D36F4, 0x8D36EC, 0x8D36F0, 0x8D3718, 0x8D3734, 0x8D373C, 0x8D3744)):
            raise Unsupported('已完成的文档包含附加通道，本版未导出。')
        return tech, count, x, y

    def _points(self, address, header, live):
        m = self.memory
        tech, count, x, y = header(address)
        # Leave one point of margin while the producer is active. On completion,
        # the finalized document supplies the last point.
        n = max(0, count - 1) if live else count
        first_x, first_y = m.read(x, 4 * n), m.read(y, 4 * n)
        second_x, second_y = m.read(x, 4 * n), m.read(y, 4 * n)
        if header(address) != (tech, count, x, y) or first_x != second_x or first_y != second_y:
            raise Waiting('采样缓冲区正在变化，等待一致快照。')
        points = tuple(zip(struct.unpack(f'<{n}f', first_x), struct.unpack(f'<{n}f', first_y)))
        validate_points(points)
        return tech, points

    def _locate(self, live):
        m = self.memory
        needle = struct.pack('<I', m.absolute(FILE_VTABLE if live else DOC_VTABLE))
        found, unsupported = [], None
        for hit in m.find(needle):
            address = hit - 0xCC0 if live else hit
            if address < 0x10000:
                continue
            try:
                header = self._live_header if live else self._doc_header
                header(address)
                self._points(address, header, live)
                found.append(address)
            except Unsupported as error:
                unsupported = error
            except (Waiting, ValueError, OSError):
                continue
        if len(found) == 1:
            return found[0]
        if len(found) > 1:
            raise Waiting('发现多个数据缓冲区；请仅打开一个实验窗口。')
        if unsupported:
            raise unsupported
        raise Waiting('等待 CHI 开始 CA、i–t 或 CP 测试。')

    def snapshot(self):
        m = self.memory
        running = m.u32(m.absolute(RUN_FLAG)) != 0
        if running and not self.was_running:
            self.epoch += 1
            self.run_key = f'{m.pid}-{time.time_ns()}-{self.epoch}'
            self.live_address = None
            self.finished = False
            self.last_scan = 0
            self.finish_since = None
        if not running and self.was_running:
            self.finish_since = time.monotonic()
        self.was_running = running
        if running:
            if self.live_address is None:
                if time.monotonic() - self.last_scan < 2:
                    raise Waiting('等待定位当前测试缓冲区。')
                self.last_scan = time.monotonic()
                self.live_address = self._locate(True)
                self.doc_address = m.u32(self.live_address - 0x3120)
            try:
                tech, points = self._points(self.live_address, self._live_header, True)
            except Waiting:
                self.live_address = None; raise
            if m.u32(m.absolute(RUN_FLAG)) == 0:
                raise Waiting('测试刚结束，等待最终数据。')
            self.last_live = Snapshot(self.run_key, tech, points, 'CHI采样缓冲区（只读快照）')
            return self.last_live
        if not self.run_key:
            raise Waiting('已连接 CHI；请在原软件中开始测试。')
        if self.finished:
            raise Waiting('本次测试已结束并保存，等待下一次测试。')
        if self.finish_since is not None and time.monotonic() - self.finish_since < 0.5:
            raise Waiting('等待 CHI 完成数据整理。')
        if self.doc_address is None:
            self.doc_address = self._locate(False)
        try:
            tech, points = self._points(self.doc_address, self._doc_header, False)
        except Waiting:
            self.doc_address = None
            raise
        # The just-finished document must overlap the live sample stream exactly.
        if self.last_live and self.last_live.points:
            old = dict(self.last_live.points)
            overlap = [(t, y) for t, y in points if t in old]
            if not overlap or any(old[t] != y for t, y in overlap) or tech != self.last_live.technique:
                self.doc_address = None
                raise Waiting('最终文档尚未与实时数据匹配，已保存的数据保持不变。')
        if m.u32(m.absolute(RUN_FLAG)) != 0:
            raise Waiting('下一次测试刚开始，等待新快照。')
        self.finished = True
        return Snapshot(self.run_key, tech, points, 'CHI测试结束后的完整数据', True)
