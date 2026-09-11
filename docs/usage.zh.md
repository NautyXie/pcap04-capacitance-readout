# PCAP04 前端使用说明

固件 **v3.1.0** + 主机端 `pcap04.py`（库）/ `pcap.py`（命令行）/ `pcap_gui.py`（图形界面）。

装依赖：`pip3 install pyserial`。

**接线：一根 USB-C 线就够了。** 从 v3.1.0 起固件自带 USB CDC 控制台，
供电、控制台、烧录全走板子自己的 USB-C（J11 跳帽必须在）。
Debug Probe 不再需要 —— 但 J10 上的 UART 控制台仍然并行工作，作为后备。

主机端按 VID/PID (`1209:0001`) 认板子自己的口，不会误抓 Debug Probe 的口。

---

## 1. 三通道悬浮 / 接地

```bash
./pcap.py -m f read          # 悬浮：3 对电极
./pcap.py -m g read          # 接地：6 个单端电极
```

```
ch0 PC0/PC1 J2/J3  --- open ---
ch1 PC2/PC3 J4/J5  --- open ---
ch2 PC4/PC5 J6/J7     99.9706 pF   (ratio 2.490443)
```

通道编号：

| 模式 | 通道 | PCAP04 端口 | 板上连接器 |
|---|---|---|---|
| 悬浮 | ch0 / ch1 / ch2 | PC0+PC1 / PC2+PC3 / PC4+PC5 | J2+J3 / J4+J5 / J6+J7 |
| 接地 | ch0…ch5 | PC0…PC5 | J2…J7 |

**运行时切换约 30 ms**（只改 3 个配置寄存器 + INIT + CDC_START），不用重新上传 548 字节固件。
只有掉电或芯片复位之后才需要完整重载，加 `--reload`。

实测（CTEST1 的 100 pF 跨在 PC4/PC5）：

```
悬浮  ch2 = 99.87 / 99.96 / 99.97 pF          对着 100 pF 标准
接地  12.81 / 13.94 / 17.45 / 13.78 / 132.36 / 134.82 pF
      前四个是裸电极走线电容，PC4/PC5 偏高是对的 —— CTEST1 正好跨在它俩之间
```

> **没接传感器的通道读数没有意义。** 默认配置下空悬通道会顶到满量程（显示 `--- open ---`），
> 但把 C_AVRG 提高之后它们会停在某个不满量程的值（见过 13.8，折算 554 pF）。
> 那不是电容，只是补偿算法在没有被测件时的输出。工具没法可靠区分，**以你实际焊了什么为准**。

---

## 2. U5 电源开关

U5 是 TPS22918 负载开关，控制 PCAP04 的 +3V3P。

```bash
./pcap.py power on           # 开（同时使能 U6 SPI 缓冲器）
./pcap.py power off          # 先把 SPI 缓冲器置高阻，再断电
./pcap.py power cycle        # 完整上下电
```

```
  +3V3P up in 4 ms, settled at 3.297 V
```

`power off` 会先 `bus off` 再断电，避免 SPI 缓冲器往一颗没供电的芯片上灌电流。
放电时间实测约 43 ms（RQOD1 470 Ω + 开关内部 25 Ω 对 20 µF），所以 `power_cycle()`
默认断电 200 ms 是充裕的。

**掉电之后 PCAP04 的 SRAM 固件就没了**，配置寄存器全变 0。`health` 会看到
`RUNBIT CLEAR - chip is idle`。下一条命令加 `--reload` 恢复：

```bash
./pcap.py power cycle
./pcap.py -m f --reload read
```

---

## 3. 连续采集与记录

```bash
./pcap.py stream             # 实时打印，Ctrl-C 停
./pcap.py stream -n 200
./pcap.py log run.csv -t 60  # 60 秒到 CSV，同时滚动显示均值/标准差
```

**采样由固件里的 INTN 引脚下降沿触发**，每一帧都是一次真正的新转换，跑在芯片自己的
转换速率上（默认 12.8 Hz），不再受控制台一问一答限制（原来约 3 Hz）。
这也从根上堵死了"轮询快于转换速率导致重复采样、标准差算出来是 0"的坑。

CSV 每行包含：主机时间、板上毫秒时标、各通道原始 32 位值 / 比值 / pF、三个状态寄存器、错误标志。

实测 40 个样本：

```
  ch2 PC4/PC5 J6/J7  mean 100.001896 pF  sd 0.0051 (51.0 ppm)  drift +0.00493  n=40
```

---

## 4. 标定（拿到真正的 pF）

```bash
./pcap.py calib 100 -c 2              # 用 ch2 上已知的 100 pF 标定
./pcap.py calib 100 -c 2 --sweep      # 顺便把整个参考电容阵列扫一遍（约 2 分钟）
```

结果存 `pcap04_calibration.json`，之后所有命令自动加载并直接显示 pF。

```
  Cref(N=31) = 40.1417 pF
  ch2 trace-to-ground parasitic (removed by C_COMP_EXT) = 22.138 pF
```

**不要用数据手册公式算 Cref。** 手册给 `0.959·N + 3.23 pF`，这颗实测是
`≈0.975·N + 9.91 pF` —— 斜率对得上（差 1.7 %），但多了约 +7 pF 的固定寄生，
而且步长在 0.6…1.2 pF 之间起伏，对仿射模型残差 ±4 %。所以标定值是**按 C_REF_SEL 码逐档存的**，
不是算出来的。做绝对测量时要在你实际使用的那一档上标定。

---

## 5. 速度 / 分辨率档位

```bash
./pcap.py speed fast         # C_AVRG=32,  CONV_TIME=2000
./pcap.py speed balanced     # C_AVRG=128, CONV_TIME=2000
./pcap.py speed precise      # C_AVRG=256, CONV_TIME=8000
```

设完之后会实测速率和噪声：

```
fast       rate 12.77 Hz   sd 242 ppm   noise 24.2 fF
balanced   rate 12.86 Hz   sd  85 ppm   noise  8.5 fF
precise    rate  3.23 Hz   sd  60 ppm   noise  6.0 fF
```

（折算到 100 pF 传感器。手册标称浮空全补偿 19 aF @ 10 Hz，那是 10 pF 基准、
最大平均深度、评估板 + C0G 电容、且转换期间不跑 SPI 的条件。本板高约 250 倍，
差异来源见 README 的噪声一节。）

**C_AVRG 不能超过 256。** 实测 ≥512 时读数系统性偏低 19.5 %（2.4911 → 2.0042），
256 和 257 都正常，所以不是"低字节为 0"的问题，更像大电容下累加器饱和。
固件和主机端都会拦下来。

---

## 6. 其他

```bash
./pcap.py health             # 解码 STATUS_0/1/2、INTN、电源轨
./pcap.py config             # 当前前端参数
./pcap.py config --refsel 24 --avrg 128 --comp ie --ports 0x3F
./pcap.py console "wr 04 B1" # 原始固件命令
```

`--comp` 取 `i`（片内）、`e`（片外）、`ie`、或空。片外补偿只在悬浮模式合法
（手册：`C_COMP_EXT` must be avoided when `C_FLOATING == 0`），接地模式下会被拒绝。

**关于"温度测量"**：这块板子上 PCAUX、PT0REF、PT1、PTOUT 全都没有走线，
所以**外部 Pt 电阻温度测量和屏蔽驱动（guard）在硬件上就用不了**。
RES6/RES7 仍会返回数值，但没有外部参考电阻，不能当绝对温度用 —— 最多当漂移指示。
如果后面要用，PCAUX 和 PT 那几个脚需要在下一版板子上引出来。

---

## 7. 库用法

```python
from pcap04 import PCap04

with PCap04() as d:
    d.power(True)
    d.load('f')                     # 或 d.mode('g') 运行时切换
    d.speed('precise')
    for s in d.stream(100):
        print(s.pf)                 # [ch0, ch1, ch2] 单位 pF
        if s.errors:
            print(s.errors)         # RUNBIT_CLEAR / PORT_ERR_PCn / ERR_OVFL ...
```

`Sample` 提供 `.raw` `.ratios` `.channels` `.pf` `.status` `.errors` `.por_flags`
`.runbit` `.is_open(i)`。

`PCap04.stats(samples, channel)` 给均值/标准差/峰峰/ppm/实测速率，
并且在所有样本完全相同时**主动报错**而不是返回标准差 0 —— 那种情况一定是重复采样。

同一个串口同时被两个进程打开时会直接报错（文件锁），不会静默地把数据搅乱。

---

## 8. 图形界面

```bash
python3 pcap_gui.py            # 连板子
python3 pcap_gui.py --sim      # 没有板子：内置模拟器，布局和功能完全一样
```

只依赖 tkinter + pyserial（模拟模式连 pyserial 都不用），不需要 matplotlib。

布局（从上到下）：

- **连接**：串口下拉 / 「模拟」勾选 / 连接-断开
- **电源 U5**：开 / 关 / 上下电，右边实时显示 +3V3P 电压（绿色 = 正常）
- **模式**：悬浮 3 对 ↔ 接地 6 电极，运行时切换；「重载固件」= 完整重传（掉电后用）
- **档位**：快速 / 平衡 / 高分辨（就是 CLI 的 speed 三档）
- **采集**：开始/停止；「录制 CSV」选文件后边采边写，列和 CLI 的 log 一致
- **设置…**：C_REF_SEL / C_AVRG / CONV_TIME / 片内外补偿 / 端口掩码
- **标定…**：填已知电容和通道，可选扫描全部 31 档；结果存文件，下次自动加载
- **健康检查**：STATUS_0/1/2 解码打到日志里
- **时间窗 / 纵轴**：自动 / **去均值（看变化，标定后单位是 fF）** / 手动范围
- 右侧每通道一张卡：大字读数、比值、σ（fF 和 ppm）、min/max、显示开关
- 底部状态栏：速率（实测）、样本数、芯片当前配置、最近错误

模拟器的标定结果存到 `pcap04_calibration_sim.json`，**不会覆盖**真板子的标定文件。

线程模型：一个工作线程独占串口，界面只投递命令、从队列取事件。
采集途中切模式/改档位，工作线程先停流、执行、再续流，不会出现两处同时用串口。


---

## 9. USB 控制台（v3.1.0 新增）

以前控制台走 UART，是因为固件里没有 USB 设备栈 —— 烧录用的 DFU 是芯片内部
ROM bootloader 自带的，跟应用固件无关。现在固件自己实现了 USB CDC-ACM。

**硬件早就支持**：CC1/CC2 有 5.1k 下拉（规范的 Type-C 受电端），D+/D- 经
USBLC6-2 直连 PA11/PA12 这两个专用引脚（不需要 SYSCFG 重映射）。板上没有晶振，
所以 USB 时钟用 HSI48 + CRS，靠主机的 SOF 包把内部 48 MHz 校准到 USB 要求的精度 ——
ROM bootloader 本来就这么跑，这条路在这块板子上早已验证过。

### 实测速率提升

`rate.py` 扫描芯片自身的触发周期，看链路实际能跑多快：

```
C_AVRG   CONV_T    标称/Hz   实测/Hz     sd/ppm     ch2/pF
    32     2000      12.8      12.9      156.3   100.0085
    32      800      31.9      32.6      181.0   100.0079
    16      400      63.8      64.4      233.3   100.0072
     8      200     127.5     115.8      409.4   100.0028
```

**从 12.8 Hz 提到 116 Hz，约 9 倍。** 原来的天花板是 UART：一帧约 90 字节，
115200 baud 下每秒最多约 128 帧，加上一问一答的往返，实际卡在 12.8 Hz。
USB 全速 12 Mbit/s 之后瓶颈回到芯片本身。

再往上（标称 255 Hz 以上）固件的取帧循环跟不上就取不到样本了 ——
每帧要做 8 次 SPI 结果读取加文本格式化，帧周期 4 ms 时来不及。
116 Hz 对电容传感来说已经远超需要，没有继续追。

注意读数在 9 倍速率变化下保持 100.007 ± 0.005 pF，噪声按预期随速率上升
（平均次数变少）。

### 诊断

```bash
./pcap.py console usb
```

```
  state      : configured
  host port  : open (DTR asserted)
  bus resets : 2
  tx dropped : 0 bytes
```

`tx dropped` 不为零说明主机没在读、固件的发送缓冲满了。**控制台永远不会因此阻塞** ——
满了就丢字节，否则一个关掉的终端就能冻住正在进行的测量。

### 踩过的一个坑

第一版烧进去之后 USB 枚举正常、命令也能跑，但**打开串口要 50 秒**，而且每次都是
50 秒左右这么稳定 —— 这种"稳定的长延时"是超时重试的特征，不是拥塞。

原因：`SET_LINE_CODING` 是一个带数据阶段的主机→设备控制传输，三个阶段
SETUP → OUT 数据 → 我们回一个零长度 IN 作为状态阶段。第一版收了数据就重新
布防接收端点，**从没发那个状态包**。主机不会报错，只会重试到超时。
macOS 打开 tty 时正好会发这条命令，所以症状就是开口要 50 秒。

补上状态阶段之后，**打开时间从 52 s 变成 0.017 s**。

（这个症状和之前 Debug Probe 那个 hub 卡死长得一模一样，所以 `pcap04.py` 里
现在会区分：慢的是板子自己的口就提示查固件版本，是 probe 的口才提示拔插。）

### 逃生通道仍然有效

`dfu` 命令跳 ROM bootloader 之前会先 `usb_cdc_detach()`：清 DPPU 让主机看到拔出、
关掉 USB 外设和时钟。不做这一步的话 ROM 的 USB 初始化会遇到一条"已经挂着设备"的总线，
DFU 枚举可能失败 —— 那会同时废掉 `dfu` 命令和双击 RESET 的逃生通道，也就是所有回到板子的路。
实测烧录两次都正常。

UART 控制台保持并行工作，就是为了万一 USB 出问题还能进去。


---

## 10. 量程

芯片返回的是 **比值** `ratio = C / Cref`，Q5.27 格式（5 位整数 + 27 位小数），
所以满量程恒等于 **32 × Cref**，LSB 恒等于 **Cref / 2²⁷**。两端都跟着 C_REF_SEL 走。

本板实测（用 CTEST1 上的 100 pF 标定出的 Cref）：

| C_REF_SEL | Cref 实测 | 满量程 | LSB |
|---|---|---|---|
| 2  | 12.04 pF | 385 pF | 0.09 aF |
| 4  | 14.24 pF | 456 pF | 0.11 aF |
| 8  | 18.62 pF | 596 pF | 0.14 aF |
| 16 | 27.19 pF | 870 pF | 0.20 aF |
| 24 | 34.97 pF | 1119 pF | 0.26 aF |
| 31 | 40.14 pF | **1285 pF** | 0.30 aF |

**用片内参考时，量程上限约 1285 pF**（C_REF_SEL=31）。手册标称的
"1pF 到 100nF" 需要**外部参考电容** —— 手册自己也写了"Internal reference 1pF to 31pF"。

要超过 1285 pF 就得设 `C_REF_INT=0` 用外部参考，参考电容接在 **PC0/PC1**，
代价是浮空通道从 3 个变成 2 个（PC2/PC3 和 PC4/PC5）。

下限不是由 LSB（亚阿法）决定的，而是由噪声：默认配置下约 13 fF RMS（见第 5 节），
提高 C_AVRG 可以压到 4 fF 左右。另外接地模式下每个电极自带 12–17 pF 走线电容做底，
浮空模式下那 22 pF 走线寄生会被片外补偿扣掉。

---

## 11. 接真实传感器：DISCHARGE_TIME 这个坑

**症状**：把液位计接到 PC2/PC3，通道读数钉死在满量程，`health` 显示
`STATUS_2 = 0x0C → PortErr(PC2), PortErr(PC3)`。同一块板子上 PC4/PC5 的
100 pF C0G 一切正常。

**不是量程问题**：探头约 70 pF（实测含线缆 96 pF），满量程 1285 pF；
而且把 C_REF_SEL 从 31 一路降到 8，读数纹丝不动仍是 32.000000 —— 真的超量程的话
换参考档位读数会变。

**原因**：ScioSense 标准配置里 **DISCHARGE_TIME = 0**。手册对 C_PortError 的解释是
"短路到地、**放电电阻太大**、电容太大、或者预充/满充/放电时间设置不当"。
零放电时间对付一颗短走线上的 C0G 没问题，但真实探头带线缆、带有损介质，
零时间放不完电，端口就报错、结果钉在满量程 —— 看起来就像"这个通道坏了"。

**实测**：

```
DISCHARGE_TIME   速率      ch1(探头)   ch2(100pF)   状态
      0         12.85 Hz   1284.5 pF    99.55 pF   PortErr PC2,PC3   ← 钉在满量程
      2         12.86 Hz     96.61 pF   99.73 pF   ok
      8         12.86 Hz     96.59 pF   99.97 pF   ok
     10          6.46 Hz     96.31 pF   99.99 pF   ok   ← 转换塞不进一个触发周期，速率减半
```

**固件 v3.2.0 起默认 DISCHARGE_TIME = 8** —— 清错误有余量，且不损失速率。
需要调的话：

```bash
./pcap.py config --disch 16      # 或 ./pcap.py console "disch 16"
```

超过 8 之后转换时间塞不进一个触发周期，速率会减半，`params` 和 `speed` 都会显示实测速率。

**探头本身的噪声比标准电容大得多**：默认配置下 ch1 约 3800 ppm（364 fF），
而同一时刻 ch2 只有 129 ppm（12.9 fF）。把 C_AVRG 提到 256 之后 ch1 降到
1260 ppm（121 fF），ch2 降到 43 ppm。探头是有损器件、又挂着线缆，这个差距是真实的，
不是配置问题 —— 要更低的噪声就得在探头和线缆上想办法（缩短线缆、加屏蔽）。


---

## 12. 上下电 / 重连的逻辑（v3.3.0 修复）

**症状**：`power off` 再 `power on` 之后，`params` 仍然说
"firmware in PCAP04 SRAM : loaded"，`read()` 也照样返回一组看起来很正常的读数 ——
但那是**上一次的旧值**。

**根因**：固件里的 `g_loaded` 标志只在 `load` 时置 1，从来没有被清零过。
掉电把 PCAP04 的 SRAM 和配置寄存器全清了，标志却还留着；主机端的
`_ensure_loaded()` 信了这个标志，就不会重新加载。

更糟的是 **POR 只清配置寄存器，不清结果寄存器** —— 芯片已经死了，
`resd` 却还能读回上一次的转换结果。数据看着完全合理，实际是陈旧的。
这是最难发现的一类故障。

**修复**：

- `rail on` / `rail off` / `safe_shutdown` 都会清 `g_loaded`
- 新增 `pcap_loaded_now()`：不光看标志，还要**问芯片自己** —— 读 cfg 0x2F 的 RUNBIT。
  POR 之后 RUNBIT 必然是 0。这样连"看门狗自己把芯片复位了"也能抓到
- `params` 里芯片空白时不再从清零的 cfg 0x04 推断模式（那会让主机悄悄切成 6 通道接地解读），
  改为显示"intended; chip is blank"
- `resd` 发现 RUNBIT 清零时明确警告结果是陈旧的
- 主机端 `read()` 遇到 RUNBIT 清零直接抛异常，不再返回旧值（要看可以传 `allow_stale=True`）；
  `collect()` / `stream()` 会自动重新加载

修复后的实测：

```
2. rail off, rail on, then a plain read (no --reload):
   params loaded=False  mode=FLOATING          ← 不再误报
   read() correctly refused: the PCAP04 has been reset ...
3. does collect()/stream() recover by itself?
   collect() -> [0.138, 642.238, 99.979]  (auto-reloaded)   ← 自动恢复
```

### 顺带修掉一个我自己引入的回归

第一版修复把 `pcap_loaded_now()` 直接放进了 `cmd_results()` 的开头。
那多出一次配置读，也就多一个 SSN 上升沿；标准配置里 `EN_ASYNC_RD` 是开的，
芯片会把"上一个值已被读走"当成可以更新结果寄存器的信号，于是我们读到一半它就改了。
实测 `resd` 路径上 ch0 在 0.128 → 0.276 pF 之间跳，而 INTN 门控的 `stream` 路径纹丝不动。

改成用 `cmd_results` 本来就要读的 STATUS_0 里的 RUNBIT 位，不额外发 SPI 事务。
修复后两条路径读数一致（0.130 pF 稳定）。

**教训：测量路径上不要插入额外的 SPI 事务。**

---

## 13. 液位计要用接地模式，不是悬浮模式

接地模式下逐个电极测出来：

```
   PC0 J2      13.548 pF     （空，基线 12.81）
   PC1 J3     363.989 pF     ← 液位计在这里
   PC2 J4      18.168 pF     （空，基线 17.45）
   PC3 J5      14.499 pF     （空，基线 13.78）
   PC4 J6     135.256 pF     （CTEST1 的 100 pF）
   PC5 J7     138.053 pF
```

**液位计是对地的单端传感器**（探头对罐壁/地），不是浮空传感器。

在**悬浮模式**下 ch0 测的是 PC0 和 PC1 **之间**的差分电容，而且片外补偿
（`C_COMP_EXT`）恰恰会把每个节点**对地**的电容扣掉 —— 而对地电容正是液位计的信号本身。
所以悬浮模式下 ch0 只读到 0.130 pF，看起来像"没接"。

**接地模式下同一个探头读 364 pF，噪声 sd 0.013 pF（36 ppm）** —— 比它挂在悬浮通道上时的
3800 ppm 好两个数量级。单端探头就该用单端模式测。

```bash
./pcap.py -m g read          # 六个电极分别对地
```

364 pF 也在量程内（片内参考 N=31 时满量程 1285 pF）。

**判断依据**：如果传感器的一端接的是地/罐体/机壳，那就是接地型，用 `-m g`；
只有两端都悬空（比如跨在 CTEST1 上的那颗 100 pF）才是浮空型，用 `-m f`。
