# SPDX-License-Identifier: MIT
"""PCAP04 Rev.6 configuration fields (datasheet section 6.3).

Only named user configuration bits are writable. Factory trim, reserved,
mandatory internal bits and nonvolatile-memory operations are not parameters.
Wire commands use hex; UI/API values are integers. No NVRAM commands are issued.
"""
from dataclasses import dataclass
import re
import time

DATASHEET = 'https://www.sciosense.com/wp-content/uploads/2023/12/PCAP04-Datasheet.pdf'


@dataclass(frozen=True)
class Field:
    name: str
    address: int
    bit: int
    width: int
    group: str
    help: str = ''
    choices: tuple = ()

    @property
    def maximum(self):
        return (1 << self.width) - 1

    def get(self, registers):
        size = (self.bit + self.width + 7) // 8
        word = sum(registers[self.address + i] << (8*i) for i in range(size))
        return (word >> self.bit) & self.maximum

    def put(self, registers, value):
        if type(value) is not int or not 0 <= value <= self.maximum:
            raise ValueError('%s: expected integer 0..%d' % (self.name, self.maximum))
        if self.choices and value not in self.choices:
            raise ValueError('%s: allowed values %s' % (self.name, self.choices))
        for i in range(self.width):
            a, b = divmod(self.bit + i, 8)
            mask = 1 << b
            registers[self.address+a] = (registers[self.address+a] & ~mask) | (((value >> i) & 1) << b)


FIELDS = {}
def field(name, address, bit, width, group, help='', choices=()):
    FIELDS[name] = Field(name, address, bit, width, group, help, tuple(choices))

# Group order also controls the GUI tabs. Integer encodings follow detailed tables,
# not the abbreviated overview (which has typos for CFG17 and PG4_INTN_EN).
field('RCHG_SEL', 3, 0, 1, '充放电', '预充电电阻: 0=10 kΩ, 1=180 kΩ')
field('PRECHARGE_TIME', 14, 0, 10, '充放电', '经电阻预充电时间；1023=禁用；单位见时钟设置')
field('FULLCHARGE_TIME', 16, 0, 10, '充放电', '无电阻最终充电时间；1023=禁用')
field('DISCHARGE_TIME', 12, 0, 10, '充放电', '放电测量窗口；不是充电斜率；1023 的描述见手册 §6.3.10/7.2.3.2')
field('RDCHG_INT_SEL0', 2, 4, 2, '充放电', 'PC0–3/PC6 放电电阻: 0/1/2/3=180/90/30/10 kΩ')
field('RDCHG_INT_SEL1', 2, 6, 2, '充放电', 'PC4–5 放电电阻: 0/1/2/3=180/90/30/10 kΩ')
for n,a,b,h in [('RDCHG_INT_EN',2,3,'内部放电电阻'),('RDCHG_EXT_EN',2,1,'PCAUX 外部电阻开关；需 AUX_PD_DIS=1'),('AUX_PD_DIS',3,6,'禁用 PCAUX 下拉'),('AUX_CINT',3,5,'内部补偿时启用 PCAUX'),('RDCHG_PERM_EN',3,2,'内部放电电阻永久接通'),('RDCHG_EXT_PERM',3,1,'外部放电永久接通；需 AUX_PD_DIS=1')]:
    field(n,a,b,1,'充放电',h)
for n,a,b,w,h,c in [
 ('C_REF_INT',4,7,1,'1=片内参考；0=外部参考',()),('C_REF_SEL',17,2,5,'参考电容档；切换需检查 pF 标定',()),
 ('C_COMP_EXT',4,5,1,'片外补偿；仅 floating',()),('C_COMP_INT',4,4,1,'片内补偿',()),
 ('C_DIFFERENTIAL',4,1,1,'差分；标准 GUI pF 解码不适用',()),('C_FLOATING',4,0,1,'0=grounded, 1=floating',()),
 ('C_PORT_EN',6,0,6,'PC0..5 使能掩码',()),('C_AVRG',7,0,13,'0/1=一次；本板 >256 尚未验证，≥512 曾低读',()),
 ('C_FAKE',15,2,4,'转换前的预热周期数',()),('CONV_TIME',9,0,23,'定时触发周期=2n/fOLF；不等于充电时间',()),
 ('C_TRIG_SEL',13,2,3,'0连续 1读触发 2定时 3拉伸定时 5引脚 6指令 7实验连续',(0,1,2,3,5,6,7)),
 ('C_STARTONPIN',13,6,2,'0..3=PG0..3',()),('C_PORT_PAT',5,5,1,'交替端口顺序；C_AVRG+C_FAKE 应为偶数',()),
 ('C_DC_BALANCE',5,0,1,'差分 floating 的 DC 平衡',()),
 ('CY_PRE_MR1_SHORT',5,7,1,'缩短内部延迟；建议0',()),('CY_PRE_LONG',5,1,1,'附加内部延迟；建议0',())]:
    field(n,a,b,w,'CDC 测量',h,c)
for n,a,b,w,h,c in [
 ('CY_HFCLK_SEL',5,3,1,'周期时钟: 0=OLF, 1=OHF',()),('CY_DIV4_DIS',5,2,1,'OHF: 0=时钟周期×4, 1=不分频',()),
 ('OLF_FTUNE',0,2,4,'低频微调；建议7，实际频率需测量',()),('OLF_CTUNE',0,0,2,'0/1/2/3≈10/50/100/200 kHz',()),
 ('OX_DIS',1,7,1,'禁用高频振荡器',()),('OX_DIV4',1,5,1,'0≈2 MHz；1≈0.5 MHz',()),
 ('OX_RUN',1,0,3,'0关 1常开 2/3/6=31/2/1 个 OLF 周期延迟',(0,1,2,3,6)),
 ('WD_DIS',28,0,8,'0x5A禁用；其他值启用9–15s看门狗',()),
 ('INT_TRIG_BG',34,7,1,'读操作触发 bandgap',()),('DSP_TRIG_BG',34,6,1,'DSP触发 bandgap',()),
 ('BG_PERM',34,5,1,'bandgap 常开，增加功耗',()),('AUTOSTART',34,4,1,'上电自动开始',()),
 ('RUNBIT',47,0,1,'0停止，1允许运行；应用过程临时清零',())]:
    field(n,a,b,w,'时钟与运行',h,c)
for n,a,b,w,h in [('C_G_OP_RUN',18,7,1,'0常开 1转换间休眠'),('C_G_OP_EXT',18,6,1,'外部 guard 放大器；需要对应硬件'),('C_G_EN',18,0,6,'逐端口 guard 使能；不是将外屏蔽接 GND'),('C_G_OP_VU',19,6,2,'0..3=1.00..1.03'),('C_G_OP_ATTN',19,4,2,'衰减编码；手册表29与表70单位不一致'),('C_G_TIME',19,0,4,'预充电切换延迟；必须小于 PRECHARGE_TIME'),('C_G_OP_TR',20,0,3,'驱动电流调节；7推荐')]:
    field(n,a,b,w,'Guard',h)
for n,a,b,w,h,c in [('R_CY',20,7,1,'RDC周期选择',()),('R_TRIG_PREDIV',21,0,10,'RDC触发分频；0/1均为1',()),('R_TRIG_SEL',22,4,3,'0关 1定时 3引脚 5CDC异步 6CDC同步',(0,1,3,5,6)),('R_AVRG',22,2,2,'0/1/2/3=1/4/8/16次平均',()),('R_PORT_EN',23,6,2,'外部PT0REF/PT1使能',()),('R_PORT_EN_IMES',23,5,1,'内部铝温度计',()),('R_PORT_EN_IREF',23,4,1,'内部参考电阻',()),('R_FAKE',23,2,1,'0=2次 1=8次预热',()),('R_STARTONPIN',23,0,2,'0..3=PG0..3',())]:
    field(n,a,b,w,'RDC 温度',h,c)
for n,a,b,w,h,c in [('I2C_A',0,6,2,'I²C地址补位；本板使用SPI',()),('DSP_MOFLO_EN',27,6,2,'GPIO去抖 0关 3开',(0,3)),('DSP_SPEED',27,2,2,'0最快..3最慢；2推荐',()),('PG1xPG3',27,1,1,'DSP脉冲引脚交换',()),('PG0xPG2',27,0,1,'DSP脉冲引脚交换',()),('DSP_STARTONPIN',29,4,4,'DSP引脚触发掩码',()),('DSP_FF_IN',29,0,4,'DSP flip-flop输入',()),('PG5_INTN_EN',30,7,1,'本板stream依赖PG5 INTN',()),('PG4_INTN_EN',30,6,1,'将INTN路由至PG4',()),('DSP_START_EN',30,0,3,'bit0 CDC / bit1 RDC / bit2 timer',()),('PG_DIR_IN',33,4,4,'PG0..3: 1输入 0输出',()),('PG_PU',33,0,4,'PG0..3上拉',())]:
    field(n,a,b,w,'DSP 与接口',h,c)
for i,a in [(0,31),(1,32)]:
    field('PI%d_TOGGLE_EN'%i,31,6+i,1,'PWM/PDM','翻转输出')
    field('PI%d_RES'%i,a,4,2,'PWM/PDM','0/1/2/3=10/12/14/16 bit')
    field('PI%d_PDM_SEL'%i,a,3,1,'PWM/PDM','0 PWM / 1 PDM')
    field('PI%d_CLK_SEL'%i,a,0,3,'PWM/PDM','0关 1/2/3 OLF÷1/2/4 4/5/6 OX÷1/2/4',range(7))
for n,a,b,w,h in [('CDC_GAIN_CORR',35,0,8,'标准DSP增益修正；会改变标定'),('BG_TIME',38,0,8,'固件定义；建议0'),('PULSE_SEL1',39,4,4,'脉冲接口1结果选择；0..7'),('PULSE_SEL0',39,0,4,'脉冲接口0结果选择；0..7'),('C_SENSE_SEL',40,0,8,'仅linearize DSP固件'),('R_SENSE_SEL',41,0,8,'仅linearize DSP固件'),('ALARM1_SELECT',42,7,1,'仅linearize DSP固件'),('ALARM1_POLARITY',42,6,1,'仅linearize DSP固件'),('ALARM0_SELECT',42,5,1,'仅linearize DSP固件'),('ALARM0_POLARITY',42,4,1,'仅linearize DSP固件'),('EN_ASYNC_READ',42,3,1,'读过结果后才更新'),('R_MEDIAN_EN',42,1,1,'仅linearize DSP固件'),('C_MEDIAN_EN',42,0,1,'仅linearize DSP固件')]:
    field(n,a,b,w,'DSP 算法',h,range(8) if n.startswith('PULSE_SEL') else ())


def decode(registers):
    return {name: f.get(registers) for name, f in FIELDS.items()}


def plan(registers, changes):
    result = dict(registers)
    for name, value in changes.items():
        if name not in FIELDS:
            raise ValueError('Unknown or protected field: %s' % name)
        FIELDS[name].put(result, value)
    p = decode(result)
    # Validate final combinations when relevant fields are explicitly changed.
    def touched(*names): return bool(set(names).intersection(changes))
    if touched('C_FLOATING','C_COMP_EXT') and p['C_COMP_EXT'] and not p['C_FLOATING']:
        raise ValueError('C_COMP_EXT requires C_FLOATING=1')
    if touched('C_PORT_PAT','C_AVRG','C_FAKE') and p['C_PORT_PAT'] and (max(1,p['C_AVRG'])+p['C_FAKE']) % 2:
        raise ValueError('Alternating ports require even C_AVRG + C_FAKE')
    if touched('RDCHG_EXT_EN','RDCHG_EXT_PERM','AUX_PD_DIS') and (p['RDCHG_EXT_EN'] or p['RDCHG_EXT_PERM']) and not p['AUX_PD_DIS']:
        raise ValueError('External discharge requires AUX_PD_DIS=1')
    if touched('C_G_EN','PRECHARGE_TIME','C_G_TIME','OX_DIS','OX_RUN','OX_DIV4','CY_HFCLK_SEL') and p['C_G_EN']:
        if not (0 < p['PRECHARGE_TIME'] < 1023 and p['PRECHARGE_TIME'] > p['C_G_TIME'] and p['CY_HFCLK_SEL'] and not p['OX_DIS'] and p['OX_RUN'] and not p['OX_DIV4']):
            raise ValueError('Guard requires enabled precharge > C_G_TIME and active, undivided OHF')
    return result


class RegisterError(RuntimeError):
    pass


class RegisterAccess:
    """Uses the existing MCU cfg/rd/wr/init/start protocol. Caller serializes I/O."""
    def read_registers(self):
        out = self.cmd('cfg')
        regs = {}
        for line in out.splitlines():
            m = re.fullmatch(r'\s*([0-9A-Fa-f]{2}):((?:\s+[0-9A-Fa-f]{2}){16})\s*', line)
            if m:
                base = int(m[1],16)
                regs.update({base+i: int(v,16) for i,v in enumerate(m[2].split())})
        if set(regs) != set(range(64)):
            raise RegisterError('Incomplete configuration read; no values assumed: '+out)
        return regs

    def read_fields(self):
        return decode(self.read_registers())

    def _write_checked(self, address, value):
        out = self.cmd('wr %02X %02X' % (address,value), _retry=False)
        pat = r'cfg\[0x%02X\]\s*<-\s*0x%02X, read back 0x([0-9a-f]{2})' % (address,value)
        m = re.search(pat,out,re.I)
        if not m or int(m[1],16) != value:
            raise RegisterError('Write verification failed at 0x%02X: %s' % (address,out))

    def write_fields(self, changes, expected=None):
        """Stop, masked write, read back, INIT; preserve requested RUNBIT.

        No implicit power/load, no automatic rollback/restart after failure.
        expected is an optimistic snapshot of edited fields (GUI stale-edit check).
        """
        before = self.read_registers()
        for name, value in (expected or {}).items():
            if name in changes and FIELDS[name].get(before) != value:
                raise RegisterError('%s changed since read; refresh before applying' % name)
        after = plan(before, changes)  # validate the whole batch before any write
        addresses = [a for a in sorted(before) if before[a] != after[a] and a != 47]
        if not addresses and before[47] == after[47]:
            return {'registers': before, 'changed': [], 'running': bool(before[47]&1)}
        self._register_stopped = True
        try:
            self._write_checked(47,before[47] & ~1)
            for a in addresses:
                self._write_checked(a,after[a])
            # Verify complete configuration while stopped, excluding RUNBIT.
            verify = self.read_registers()
            bad = [a for a in before if a != 47 and verify[a] != after[a]]
            if bad:
                raise RegisterError('Configuration changed/readback mismatch: %s' % bad)
            self._write_checked(47,after[47])
            if after[47] & 1:
                for cmd in ('init','start'):
                    out = self.cmd(cmd,_retry=False)
                    if 'sent 0x'+('8A' if cmd=='init' else '8C') not in out:
                        raise RegisterError('Restart not acknowledged: '+out)
                    time.sleep(0.02)
            verify = self.read_registers()
            if verify != after:
                raise RegisterError('Configuration mismatch after restart')
        except Exception as e:
            try:
                self._write_checked(47,before[47] & ~1)
                state = 'RUNBIT cleared; inspect and reload before acquisition'
            except Exception:
                state = 'Unable to confirm RUNBIT; inspect hardware state'
            raise RegisterError('%s. %s. Partial changes may remain.' % (e,state)) from e
        self._register_stopped = not bool(after[47]&1)
        if set(changes).intersection({'C_REF_INT','C_DIFFERENTIAL','C_FLOATING','C_COMP_INT','C_COMP_EXT','CDC_GAIN_CORR'}):
            self.cal = None  # persisted calibration remains intact; revalidate before reuse
        self._mode = 'f' if FIELDS['C_FLOATING'].get(after) else 'g'
        if getattr(self,'cal',None):
            self.cal.refsel = FIELDS['C_REF_SEL'].get(after)
        return {'registers': verify, 'changed': addresses + ([47] if before[47]!=after[47] else []), 'running': bool(after[47]&1)}

    def set_precharge(self, n): return self.write_fields({'PRECHARGE_TIME': n})
    def set_fullcharge(self, n): return self.write_fields({'FULLCHARGE_TIME': n})
    def set_charge_resistor(self, n): return self.write_fields({'RCHG_SEL': n})
