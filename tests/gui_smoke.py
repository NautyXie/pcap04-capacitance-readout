"""Native Tk smoke test; opens a simulator only, never a serial port."""
import sys
import time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'host'))
from pcap_gui import App
from pcap_registers import FIELDS

app=App(sim=True)
def until(check,timeout=5):
    end=time.monotonic()+timeout
    while not check():
        app.update();time.sleep(.02)
        if time.monotonic()>end:raise AssertionError('GUI timed out')
    app.update()
try:
    until(lambda: app.worker is not None and not app.busy)
    app._call('load f',lambda:(app.dev.power(True),app.dev.load('f'))[-1])
    until(lambda:not app.busy,8)
    app._dlg_settings()
    d=app.settings_dialog
    until(lambda:d.registers is not None and not d.pending)
    assert len(d.vars)==90
    d.vars['PRECHARGE_TIME'].set('50')
    d.vars['RCHG_SEL'].set('1')
    d.apply()
    until(lambda:not d.pending)
    assert d.vars['PRECHARGE_TIME'].get()=='50',d.status.get()
    assert FIELDS['RCHG_SEL'].get(d.registers)==1
    assert not app.worker.streaming
    d.vars['RUNBIT'].set('0');d.apply();until(lambda:not d.pending)
    assert not (d.registers[47]&1)
    d.close()
    print('PASS: Tk built; 90 fields read; split timing + resistor apply verified; stopped state retained.')
finally:
    app._on_close()
