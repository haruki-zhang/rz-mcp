# 流程组的详细记录（请在盲评之后再看）

> 2026-11-27。这是对照试验里"按流程构想"那一组的工作记录：能力清单、需求信号、候选、淘汰原因、打分、反方意见和回测。
> 看了这份文件就知道哪些提案出自流程组，所以请先完成 `proposals_blind.md` 的盲评。

## 1. 能力清单（简版，来自文档索引）

| 能力 | 出处 |
|---|---|
| 四核 Cortex-A55 1.2 GHz + Cortex-M33 200 MHz，可选由 M33 启动 | 数据手册 R01DS0469EJ0110 Rev 1.10 p.1–2 |
| Mali-G31 图形（可选）、H.264 编解码（可选）、图像缩放单元 | 数据手册 p.1、p.3；硬件手册 §9.7、§9.9 |
| MIPI CSI-2 一路（1 或 2 lane）；显示在 DSI、并行、LVDS 中三选一 | 数据手册 p.1 |
| 千兆以太网 2 路，硬件支持 TSN（802.1Qbv/Qbu/AS）和 IEEE 1588 时间戳 | 硬件手册 R01UH1153EJ0110 §6.3.1.1 p.1851 |
| CAN-FD 3 路；载板上有 2 路收发器和接口 | 数据手册 p.1；载板手册 R01UH1077EJ0120 §3.9 p.65、§2.2.3 p.30 |
| PCIe Gen2 一路；载板有 M.2 Key E 和 Key B | 数据手册 p.1；载板手册 §3.11 p.66–68 |
| PDM 输入 3 路；模块板上自带一个 MEMS 麦克风 | 数据手册 p.1；模块板手册 R01UH1193EJ0100 §3.18 p.42 |
| 通用 PWM 定时器 8 通道、MTU3a、POEG/POE3、12 位 ADC 8 通道 | 数据手册 p.1；硬件手册 §5.7–5.10、§7.10 |
| 硬件加密引擎 RSIP、OTP | 硬件手册 §4.8、§4.12 |
| 低功耗模式、挂起到内存、VBATT 备份域、RTC | 硬件手册 §4.5、§4.10、§4.11；电源管理驱动手册 R01US0605EJ0115 §4.7 |
| 载板上的 PMOD、Grove 接口；整机功耗监测 | 载板手册 §2 p.13、§2.2.3 p.30 |
| 转接板：LVDS-HDMI、DSI-HDMI、RGB-HDMI2、Pi Camera、Breakout | 文档目录 |
| 可选软件包：图形、视频编解码、安全（需审批）、Multi-OS | `catalog/bsp_releases.toml` |

可行性核对时没能确认的两点：

- **TSN 的调度功能（802.1Qbv）在 Linux 驱动里是否可用**：以太网驱动手册里搜不到相关内容，硬件手册只说明硬件支持。
- **摄像头模组的驱动**：文档里没有找到 BSP 支持哪些传感器型号。

## 2. 需求信号（均为 2026-11-27 检索）

| 信号 | 出处 |
|---|---|
| G3L 于 2026 年 9 月发布，定位 HMI：智能零售终端、POS、医疗 HMI；强调四核、M33、3D 图形、视频编解码、PCIe、TSN 集于一颗 | https://www.renesas.com/products/rz-g3l 、https://www.eenewseurope.com/en/renesas-launches-64-bit-rz-g3l-rz-g3se-mpus-for-hmi-and-iot-edge/ 、https://www.electronics-lab.com/news/renesas-rz-g3l-and-rz-g3se-64-bit-microprocessor-units-target-hmi-and-iot-edge-devices/ |
| 面向工业、办公、楼宇和家居设备，强调摄像头输入和带视频的控制 | https://www.futureelectronics.com/ftm/connectivity/renesas-rzg3l-rzg3se-microprocessor/ |
| 待机 1 mW 级；A55 跑 Linux、M33 做实时，Zephyr 是发布伙伴 | https://allaboutcircuits.com/news/renesas-intros-64-bit-mpu-duo-for-advanced-hmi-and-iot-edge-systems 、https://www.cnx-software.com/2026/09/11/renesas-rz-g3l-rz-g3se-ultra-low-power-cortex-a55-mpus-for-industrial-hmi-and-iot-applications/ |
| TI：AM62L 充电桩开发套件（2026 年 1 月）和充电桩 HMI 演示 | https://www.ti.com/tool/AM62L-EVSE-DEV-EVM |
| Microchip：embedded world 2026 展示充电桩 HMI、TSN、工业自动化 | https://www.microchip.com/en-us/about/events-info/embedded-world |
| NXP：embedded world 2026 发布 i.MX 93W（集成三模无线）；i.MX 94 机器人演示用到 TSN；i.MX 937 面向 HMI 和边缘 AI | https://www.hackster.io/news/nxp-unveils-the-i-mx-93w-replacing-up-to-60-discrete-components-in-physical-ai-designs-0cddef10dcf5 、https://www.nxp.com/pages/nxp-embedded-world:NXP-EMBEDDED-WORLD 、https://www.cnx-software.com/2026/03/11/nxp-i-mx-937-cost-effective-cortex-a55-m7-m33-mpu-is-a-drop-in-replacement-for-nxp-i-mx-95-soc-family/ |
| ST：STM32MP25 带 1.35 TOPS 的 AI 加速器和双口千兆 TSN 交换 | https://imveurope.com/content/st-releases-new-microprocessors-industrial-edge-ai |
| HMI 趋势：图形框架要搭配独立实时核；语音和手势在高安全和移动场景出现但未普及；网络安全成为 HMI 更新换代的决定因素 | https://promwad.com/news/flutter-aosp-industrial-embedded-hmi-real-time-constraints 、https://www.automationworld.com/control/article/55302266/ai-and-smart-sensors-in-transforming-hmi-scada-systems-in-manufacturing |
| 欧盟 CRA 覆盖工业网关，要求完整性保护和免费安全更新 | https://www.hms-networks.com/tech-blog/blogpost/hms-blog/2026/04/07/cra-is-redefining-industrial-connectivity 、https://www.nxp.com/company/about-nxp/smarter-world-blog/BL-CYBER-RESILIENCE-ACT |
| RZ/G HMI SDK 支持 LVGL、Qt、Slint 等；没有搜到针对 G3L 的伙伴演示视频 | https://renesas-rz.github.io/rzg_hmi_sdk/latest |

没有用到内部信号（工单和询价），这次只用了公开信息。

## 3. 候选和淘汰

共 23 个候选。硬门槛淘汰的：

| 候选 | 淘汰原因 |
|---|---|
| TSN 时间感知调度演示（背景流量下控制流零抖动） | 驱动手册里查不到 802.1Qbv 调度的支持，只能确认硬件支持。这是 G3L 的重点卖点，建议先核实驱动，核实后可以恢复为候选 |
| 双屏异显 | 显示接口只能三选一 |
| 板上跑大语言模型的对话助手 | 四核 A55 1.2 GHz、没有 AI 加速器，估计达不到可演示的速度 |
| 电池供电手持终端的超长待机 | 和已有的 LowPower Display 重复 |

通过门槛但分数低、没进前 8 的：GUI 框架同台对比（61）、CAN-FD 实时网关（58）、PDM 麦克风阵列声源定位（54）、纯 CPU 的视觉检测（53）、多路摄像头录像到 NVMe、Matter 家居中控屏、OPC UA 服务器、蜂窝远程 HMI、应用容器化升级、接近唤醒的自助终端。其中纯 CPU 视觉检测分低的原因是：同档竞品都有 AI 加速器，这个演示突出的是 G3L 的短处。

## 4. 前 8 的打分

满分 100：演示效果 35、需求证据 20、差异点 20、复用面 15、工作量和风险 10。

| 排名 | 提案 | 演示 | 证据 | 差异 | 复用 | 工作量 | 合计 |
|---|---|---|---|---|---|---|---|
| 1 | EV 充电桩 HMI | 27 | 18 | 14 | 13 | 8 | 80 |
| 2 | Linux 崩了，屏幕不黑 | 32 | 12 | 18 | 12 | 5 | 79 |
| 3 | 自助零售终端 | 29 | 17 | 9 | 11 | 9 | 75 |
| 3 | 可视对讲面板 | 28 | 14 | 16 | 12 | 5 | 75 |
| 5 | 界面和电机控制单芯片 | 31 | 13 | 14 | 11 | 5 | 74 |
| 6 | 常开感知、事件唤醒 | 22 | 13 | 16 | 12 | 7 | 70 |
| 7 | CRA 安全网关 | 18 | 19 | 10 | 14 | 6 | 67 |
| 7 | 离线语音 HMI | 24 | 13 | 12 | 11 | 7 | 67 |

领域分布：HMI 3 个（充电桩、崩溃不黑屏、零售终端）；网关和联网 2 个（事件唤醒、CRA 网关）；边缘 AI 和视觉 2 个（可视对讲、离线语音）；其他 1 个（电机控制）。

## 5. 反方意见

| 提案 | 反方意见 | 结论 |
|---|---|---|
| EV 充电桩 HMI | TI 和 Microchip 都已经展示过充电桩 HMI，这是跟随。差别只在"待机时 M33 维持显示"和快启，而这两样已有 PoC 展示过 | 保留。价值在于把已有成果装进客户熟悉的产品形态，新增工作量小；但不要期待它在展会上独树一帜 |
| Linux 崩了，屏幕不黑 | 没有查到同档竞品做过同样的演示，但也没有查到客户明确提出这个需求；医疗和工业的需求是推断出来的。技术上"单独复位 A55"没有验证 | 保留，但先花一周做可行性验证再决定 |
| 自助零售终端 | 任何带 GPU 的 MPU 都能做，不突出 G3L | 保留。它直接对应 Renesas 的市场定位，互动性强；差异点得分已反映这个弱点 |
| 可视对讲面板 | 对讲类演示很常见。"同档竞品没有硬件视频编码"是凭记忆的判断（i.MX 93 和 AM62x 没有硬件编解码器），没有联网核实 | 保留，开工前核实竞品规格和摄像头驱动 |
| 电机控制单芯片 | i.MX 93 和 STM32MP2 也有 M 核，可以讲同样的故事；G3L 的优势是电机专用定时器更全，但这一点观众看不出来 | 保留。会动的演示在展会上占便宜 |
| 常开感知、事件唤醒 | 演示本身比较静，要靠功耗数字说话；和 LowPower Display 共用基础 | 保留，适合客户拜访，不适合展会 |
| CRA 安全网关 | 演示效果弱，需要讲解才看得懂 | 保留。它是唯一有法规期限推动的提案，对出口欧洲的客户最有说服力。CRA 的具体日期是凭记忆写的，使用前要核实 |
| 离线语音 HMI | 展会现场嘈杂，语音演示容易失败；实时性没有验证 | 降级到最后，只推荐用于安静场合 |

## 6. 回测：过去的两个 PoC 用同一张评分表打分

| PoC | 演示 | 证据 | 差异 | 复用 | 工作量 | 合计 | 在本次 10 个里的名次 |
|---|---|---|---|---|---|---|---|
| LowPower Display | 27 | 14 | 19 | 12 | 6 | 78 | 第 3 |
| FastBoot + Seamless Logo | 28 | 15 | 12 | 15 | 7 | 77 | 第 4 |

两个都排在前一半，符合设计文档里 I4 的标准。

这个回测的说服力有限：分数是我自己打的，而且我事先知道这两个是你做过的 PoC。更可靠的做法是让一个不知情的会话按评分表独立打分。
