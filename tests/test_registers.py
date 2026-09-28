import sys
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'host'))
from pcap_registers import FIELDS, RegisterAccess, RegisterError, decode, plan


class Device(RegisterAccess):
    def __init__(self):
        self.regs=dict.fromkeys(range(64),0)
        self.regs.update({4:0xB1,7:32,9:0xD0,10:7,14:0xff,15:3,17:0x7c,28:0x5a,30:0x82,47:1,62:0xe0,63:1})
        self.calls=[]
        self.fail=None
        self.cal=None

    def cmd(self,line,**kwargs):
        self.calls.append(line)
        p=line.split()
        if p[0]=='cfg':return '\n'.join('  %02X:'%a+''.join(' %02X'%self.regs[i] for i in range(a,a+16)) for a in range(0,64,16))
        if p[0]=='wr':
            a,v=map(lambda x:int(x,16),p[1:])
            if a!=self.fail:self.regs[a]=v
            return '  cfg[0x%02X] <- 0x%02X, read back 0x%02X'%(a,v,self.regs[a])
        return '  sent 0x'+{'init':'8A','start':'8C'}[line]


class RegisterTests(unittest.TestCase):
    def test_split_fields_preserve_neighbours(self):
        d=Device();d.regs[15]=0xfc;d.regs[17]=0xfc;d.regs[13]=0xfc
        out=plan(d.regs,{'PRECHARGE_TIME':513,'FULLCHARGE_TIME':1023,'DISCHARGE_TIME':258})
        self.assertEqual((out[14],out[15]),(1,0xfe))
        self.assertEqual((out[16],out[17]),(255,0xff))
        self.assertEqual((out[12],out[13]),(2,0xfd))
        self.assertEqual(out[62],0xe0)

    def test_field_layout_no_overlap_and_roundtrip(self):
        occupied=set()
        for f in FIELDS.values():
            bits={(f.address+(f.bit+i)//8,(f.bit+i)%8) for i in range(f.width)}
            self.assertFalse(bits & occupied,f.name)
            occupied|=bits
            for value in [0,f.maximum] if not f.choices else f.choices:
                regs=dict.fromkeys(range(64),0xa5);old=regs.copy();f.put(regs,value)
                self.assertEqual(f.get(regs),value)
                for a in regs:
                    for b in range(8):
                        if (a,b) not in bits:self.assertEqual((old[a]>>b)&1,(regs[a]>>b)&1)
        self.assertTrue(all(a<48 for a,b in occupied))

    def test_transaction_and_restart(self):
        d=Device();out=d.write_fields({'RCHG_SEL':1,'PRECHARGE_TIME':50})
        self.assertEqual(decode(out['registers'])['PRECHARGE_TIME'],50)
        self.assertEqual(d.calls[1],'wr 2F 00')
        self.assertEqual(d.calls[-3:],['init','start','cfg'])
        self.assertEqual(d.regs[47],1)

    def test_explicit_stop_not_restarted(self):
        d=Device();d.write_fields({'RUNBIT':0})
        self.assertNotIn('start',d.calls)
        self.assertTrue(d._register_stopped)

    def test_failure_leaves_stopped(self):
        d=Device();d.fail=14
        with self.assertRaisesRegex(RegisterError,'Partial changes'):
            d.write_fields({'PRECHARGE_TIME':50})
        self.assertEqual(d.regs[47],0)
        self.assertNotIn('start',d.calls)

    def test_stale_edit_and_validation_do_not_write(self):
        d=Device()
        with self.assertRaises(RegisterError):d.write_fields({'C_AVRG':64},expected={'C_AVRG':1})
        for changes in [{'PRECHARGE_TIME':1024},{'RCHG_SEL':-1},{'MEM_CTRL':0xb8},{'C_TRIG_SEL':4},{'C_FLOATING':0}]:
            with self.assertRaises(ValueError):d.write_fields(changes)
        self.assertTrue(all(c=='cfg' for c in d.calls))

    def test_full_average_range(self):
        d=Device();d.write_fields({'C_AVRG':8191})
        self.assertEqual(FIELDS['C_AVRG'].get(d.regs),8191)

    def test_noop_does_not_restart(self):
        d=Device();d.write_fields({'C_AVRG':32});self.assertEqual(d.calls,['cfg'])

    def test_partial_dump_rejected(self):
        d=Device();d.cmd=lambda *a,**kw:'00: 00 00'
        with self.assertRaises(RegisterError):d.read_fields()

    def test_guard_and_external_resistor_constraints(self):
        d=Device()
        with self.assertRaises(ValueError):plan(d.regs,{'C_G_EN':1})
        with self.assertRaises(ValueError):plan(d.regs,{'RDCHG_EXT_EN':1})
        p=plan(d.regs,{'RDCHG_EXT_EN':1,'AUX_PD_DIS':1})
        self.assertEqual(decode(p)['RDCHG_EXT_EN'],1)

if __name__=='__main__':unittest.main()

class LibraryStateTests(unittest.TestCase):
    def test_stopped_edit_does_not_implicitly_load(self):
        from pcap04 import PCap04, PCapError
        d=object.__new__(PCap04)
        d._register_stopped=True
        d.params=lambda: self.fail('must not query/load after intentional stop')
        with self.assertRaises(PCapError):d._ensure_loaded()

    def test_calibration_invalidated_only_for_response_changes(self):
        d=Device()
        class Cal: refsel=31
        d.cal=Cal()
        d.write_fields({'RCHG_SEL':1})
        self.assertIsNotNone(d.cal)
        d.write_fields({'CDC_GAIN_CORR':64})
        self.assertIsNone(d.cal)
