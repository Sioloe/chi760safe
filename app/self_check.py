"""Offline checks and a visible sample export; never opens a real instrument."""
from pathlib import Path
import json
import struct
import tempfile
import unittest
from backup import BackupWriter, Snapshot, read_recovery, validate_points
from chi_memory import CHIReader, Waiting, Unsupported, FILE_VTABLE, DOC_VTABLE, RUN_FLAG, DATA_LIMIT


def recovery(tech, points):
    header = bytearray(676)
    struct.pack_into('<8i', header, 0, 105, 2026, 9, 30, 12, 0, 0, tech)
    return bytes(header) + b''.join(struct.pack('<ff', *p) for p in points)


class FakeMemory:
    """Known-address fixture; contains no data from another process."""
    pid = 12345
    obj, doc, x, y = 0x200000, 0x2000000, 0x500000, 0x600000
    def __init__(self, technique=4, points=((0.5, 0.001), (1.0, 0.002), (1.5, 0.003))):
        self.blocks = {}
        self.blocks[self.obj] = bytearray(0x3000)
        self.blocks[self.obj-0x3120] = bytearray(struct.pack('<I',self.doc))
        self.blocks[self.doc] = bytearray(0x8d9000)
        self.blocks[RUN_FLAG] = bytearray(struct.pack('<I', 1))
        self.blocks[DATA_LIMIT] = bytearray(struct.pack('<I', 131072))
        self.put(self.obj+0xcc0, FILE_VTABLE)
        self.put(self.doc, DOC_VTABLE)
        self.put(self.doc+0x8d520c, 1)
        self.put(self.obj+0x12f4, technique)
        self.put(self.doc+0x8d3920, technique)
        self.put(self.obj+0x1984, self.x); self.put(self.obj+0x1988, self.y)
        self.put(self.doc+0x8d87d4, self.x); self.put(self.doc+0x8d87dc, self.y)
        self.replace(points)

    def put(self, address, value):
        for start, block in self.blocks.items():
            if start <= address and address+4 <= start+len(block):
                struct.pack_into('<I', block, address-start, value); return
        raise KeyError(hex(address))

    def replace(self, points):
        self.blocks[self.x] = bytearray(b''.join(struct.pack('<f', p[0]) for p in points))
        self.blocks[self.y] = bytearray(b''.join(struct.pack('<f', p[1]) for p in points))
        self.put(self.obj+0x1344, len(points)); self.put(self.doc+0x8d397c, len(points))

    def read(self, address, length):
        if length == 0: return b''
        for start, block in self.blocks.items():
            if start <= address and address+length <= start+len(block):
                return bytes(block[address-start:address-start+length])
        raise Waiting('模拟缓冲区不可读')

    def u32(self, address): return struct.unpack('<I', self.read(address, 4))[0]
    def absolute(self, address): return address
    def find(self, needle):
        if needle == struct.pack('<I', FILE_VTABLE): yield self.obj+0xcc0
        elif needle == struct.pack('<I', DOC_VTABLE): yield self.doc


class Checks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.writer = BackupWriter(self.root)

    def tearDown(self):
        self.writer.close(); self.temp.cleanup()

    def rows(self, name='data.txt'):
        return (self.writer.directory/name).read_text(encoding='utf-8-sig').splitlines()

    def test_units_for_all_three_techniques(self):
        for tech, unit, expected in ((4,'电流(mA)','1'),(11,'电流(mA)','1'),(15,'电位(V)','0.001')):
            self.writer.accept(Snapshot(str(tech),tech,((0.5,0.001),),'fixture'))
            self.writer.flush()
            self.assertEqual(self.rows()[0], '序号\t时间(s)\t'+unit)
            self.assertEqual(self.rows()[1].split('\t'), ['1','0.5',expected])

    def test_repeated_snapshot_has_no_duplicate(self):
        snap=Snapshot('a',4,((0.5,1e-3),(1,2e-3)),'fixture')
        self.writer.accept(snap); self.writer.flush(); self.writer.accept(snap); self.writer.flush()
        self.assertEqual(len(self.rows()),3)

    def test_flush_interval_keeps_pending_in_memory(self):
        self.writer.accept(Snapshot('a',4,((0.5,1e-3),),'fixture'))
        self.assertEqual(self.writer.flushed,0)
        self.writer.flush(); self.assertEqual(self.writer.flushed,1)

    def test_shutdown_writes_pending_batch(self):
        self.writer.accept(Snapshot('a',15,((0.5,-0.01),(1,-0.02)),'fixture'))
        self.writer.close(); self.assertEqual(len(self.rows()),3)

    def test_new_run_keeps_previous_file(self):
        self.writer.accept(Snapshot('a',4,((0.5,0.001),),'fixture'))
        old=self.writer.directory
        self.writer.accept(Snapshot('b',15,((0.5,0.2),),'fixture'))
        self.assertNotEqual(old,self.writer.directory)
        self.assertEqual(len((old/'data.txt').read_text(encoding='utf-8-sig').splitlines()),2)

    def test_rewritten_series_is_not_mixed(self):
        self.writer.accept(Snapshot('a',4,((0.5,0.001),(1,0.002)),'fixture'))
        old=self.writer.directory
        self.writer.accept(Snapshot('a',4,((0.5,0.01),(1,0.02)),'fixture'))
        self.assertNotEqual(old,self.writer.directory)

    def test_shrinking_native_buffer_keeps_saved_detail(self):
        self.writer.accept(Snapshot('a',4,tuple((i,float(i)) for i in range(1,9)),'fixture'))
        old=self.writer.directory
        self.writer.accept(Snapshot('a',4,((2,1.5),(4,3.5),(6,5.5),(8,7.5),(10,9.5)),'fixture'))
        self.assertEqual(old,self.writer.directory)
        self.assertEqual(len(self.writer.points),9)
        self.assertEqual(self.writer.points[-1],(10,9.5))

    def test_partial_recovery_record_is_ignored(self):
        snap=read_recovery(recovery(11,((0.5,0.001),(1,0.002)))+b'\x01\x02\x03')
        self.assertEqual(len(snap.points),2)
        self.assertAlmostEqual(snap.points[1][1],0.002,places=8)

    def test_recovery_validates_version_and_channels(self):
        b=bytearray(recovery(4,((1,0.01),)))
        struct.pack_into('<i',b,0,106)
        with self.assertRaises(ValueError):read_recovery(bytes(b))
        struct.pack_into('<i',b,0,105);struct.pack_into('<i',b,52,1)
        with self.assertRaises(ValueError):read_recovery(bytes(b))

    def test_recovery_validates_technique_and_date(self):
        with self.assertRaises(ValueError):read_recovery(recovery(32,((1,0.01),)))
        b=bytearray(recovery(4,((1,0.01),)));struct.pack_into('<i',b,8,13)
        with self.assertRaises(ValueError):read_recovery(bytes(b))

    def test_invalid_samples_are_rejected(self):
        for points in (((1,float('nan')),), ((-1,0),), ((1,0),(1,1)), ((2,0),(1,1))):
            with self.assertRaises(ValueError):validate_points(points)

    def test_memory_layout_all_three_techniques(self):
        for tech in (4,11,15):
            memory=FakeMemory(tech);reader=CHIReader(memory)
            snap=reader.snapshot()
            self.assertEqual(snap.technique,tech)
            self.assertEqual(len(snap.points),2)
            self.assertAlmostEqual(snap.points[-1][1],0.002,places=8)

    def test_final_document_supplies_last_point(self):
        memory=FakeMemory();reader=CHIReader(memory)
        snap=reader.snapshot();self.writer.accept(snap)
        memory.put(RUN_FLAG,0)
        with self.assertRaises(Waiting):reader.snapshot()
        reader.finish_since=0
        last=reader.snapshot()
        self.assertTrue(last.complete);self.assertEqual(last.key,snap.key)
        self.writer.accept(last);self.writer.close()
        self.assertEqual(len(self.rows()),4)

    def test_other_technique_and_aux_channel_blocked(self):
        with self.assertRaises(Unsupported):CHIReader(FakeMemory(0)).snapshot()
        memory=FakeMemory();memory.put(memory.obj+0x19bc,1)
        with self.assertRaises(Unsupported):CHIReader(memory).snapshot()

    def test_changed_buffer_snapshot_retried(self):
        memory=FakeMemory();reader=CHIReader(memory)
        original=memory.read;counter=[0]
        def changing(address,n):
            if address == memory.x:
                counter[0]+=1
                if counter[0]%2==0:return b'\0'*n
            return original(address,n)
        memory.read=changing
        with self.assertRaises(Waiting):reader._points(memory.obj,reader._live_header,True)

    def test_new_run_gets_new_key(self):
        memory=FakeMemory();reader=CHIReader(memory)
        a=reader.snapshot();memory.put(RUN_FLAG,0)
        with self.assertRaises(Waiting):reader.snapshot()
        memory.put(RUN_FLAG,1);b=reader.snapshot()
        self.assertNotEqual(a.key,b.key)


def run(folder, ui=False):
    folder=Path(folder);folder.mkdir(parents=True,exist_ok=True)
    with (folder/'test-results.txt').open('w',encoding='utf-8') as log:
        result=unittest.TextTestRunner(stream=log,verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(Checks))
    for tech in (4,11,15):
        writer=BackupWriter(folder/'examples')
        points=tuple((i*0.5,0.0001*i if tech!=15 else -0.01*i) for i in range(1,13))
        writer.accept(Snapshot(f'synthetic-{tech}',tech,points,'模拟数据，未连接仪器',True));writer.close()
    summary={'passed':result.wasSuccessful(),'tests':result.testsRun,'errors':len(result.errors),'failures':len(result.failures),
             'hardware_tested':False,'source':'synthetic known-value fixtures; no real instrument commands'}
    import sys
    summary['frozen']=bool(getattr(sys,'frozen',False))
    if ui:
        import tkinter as tk
        from chi_backup import App
        root=tk.Tk()
        try:
            app=App(root)
            root.update_idletasks()
            summary['window_title']=root.title()
            summary['ui_size']=[root.winfo_width(),root.winfo_height()]
            summary['ui_startup']=True
        finally:
            root.destroy()
    (folder/'result.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
    if not result.wasSuccessful():raise SystemExit(1)
    return summary


if __name__=='__main__':
    import sys
    print(run(Path(sys.argv[1]) if len(sys.argv)>1 else Path('SelfTest')))
