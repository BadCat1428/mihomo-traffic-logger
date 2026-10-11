[English](README.md) | **简体中文**

# mihomo-traffic-logger

记录 Mihomo（Clash.Meta）内核每天跑了多少流量，方便事后回看。

内核只提供开机以来的累计计数，一重启就归零；GUI 上的曲线也只覆盖很短一段窗口。这个脚本盯着那两个计数，按本地日期分桶，存进一个小 JSON 文件。

无依赖，只用 Python 3.8+ 标准库。

## 为什么不做进 GUI

这个功能最初是提给 Clash Verge Rev 的（[#8175](https://github.com/clash-verge-rev/clash-verge-rev/issues/8175)）。被拒了，因为项目有明确的承诺：不收集、不分析用户流量——[#7348](https://github.com/clash-verge-rev/clash-verge-rev/issues/7348) 和 [#6938](https://github.com/clash-verge-rev/clash-verge-rev/issues/6938) 也是同样的理由关掉的。维护者建议改用内核的 External Controller，自己写个小脚本把数据取出来另存。这就是那个脚本：跑在你自己的机器上，写你自己的文件，不往任何地方发数据。

## 快速开始

指向内核的 External Controller，让它跑起来：

```bash
python3 mihomo_traffic_logger.py run \
  --controller http://127.0.0.1:9090 \
  --secret your-secret
```

随时查看历史：

```bash
python3 mihomo_traffic_logger.py report
```

```
DATE             DOWNLOAD       UPLOAD        TOTAL
2026-10-09        4.12 GB     305.71 MB       4.42 GB
2026-10-10        6.85 GB     512.30 MB       7.35 GB
2026-10-11        1.93 GB     128.44 MB       2.06 GB
TOTAL            12.90 GB     946.45 MB      13.83 GB
AVERAGE/DAY       4.30 GB     315.48 MB       4.61 GB
```

`--days N` 只看最近 N 天。

## 配置项

每个选项都有对应的环境变量，方便写服务单元时保持简短：

| 参数 | 环境变量 | 默认值 |
|---|---|---|
| `--controller` | `MIHOMO_CONTROLLER` | `http://127.0.0.1:9090` |
| `--secret` | `MIHOMO_SECRET` | 空 |
| `--data` | `MIHOMO_TRAFFIC_DATA` | 系统数据目录 |
| `--flush-interval` | | `60`，两次写盘之间的秒数 |
| `--retention` | | `0`，即全部保留 |
| `--verbose` | | 打印每次更新 |

默认历史文件位置：Windows 下是 `%APPDATA%\mihomo-traffic-logger\daily.json`，其他系统是 `$XDG_DATA_HOME/mihomo-traffic-logger/daily.json`。

## 三种模式

- `run` —— 连 `/traffic`，有流量就记，断线自动重连。默认模式。
- `sample` —— 只取一次读数就退出。如果你更想用 cron 或计划任务，而不是常驻一个进程，用这个。
- `report` —— 打印每日表格。

## 打开 External Controller

**独立运行的 mihomo** —— 写进 `config.yaml` 然后重启内核：

```yaml
external-controller: 127.0.0.1:9090
secret: "自己定一个"
```

**Clash Verge Rev** —— 它的内核走的是命名管道而不是 TCP 端口，所以生成的配置里虽然写着 `127.0.0.1:9097`，那个端口实际是不监听的。桌面端要用这个脚本，得自己另起一个能连上的内核，或者干脆放到跑着独立 mihomo 的机器上。

controller 一定要绑在 loopback。它除了 secret 之外没有别的认证，而它等于内核的完全控制权——能改代理、能改规则。

## 统计的是什么，不是什么

统计的是内核自己报的字节数。直连和走代理的都在里面，内核不区分这两者。

依赖这些数字之前，有两点得先说清楚：

- **只在脚本运行时才有记录。** 内核不保留历史，进程停掉那段时间补不回来，没有办法回填。
- **跨零点的差值会算在采样那天。** 默认 60 秒写一次，误差最多一个间隔。需要更精确的话，用 `sample` 提高采样频率。

## 作为服务运行

**systemd**（Linux，和独立 mihomo 一起跑）：

```ini
[Unit]
Description=Mihomo daily traffic logger
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=/usr/bin/python3 /opt/mihomo-traffic-logger/mihomo_traffic_logger.py run
Environment=MIHOMO_CONTROLLER=http://127.0.0.1:9090
Environment=MIHOMO_SECRET=your-secret
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

**计划任务**（Windows，每分钟取一次）：

```powershell
schtasks /create /tn "mihomo-traffic-logger" /sc minute /mo 1 ^
  /tr "pythonw.exe C:\path\to\mihomo_traffic_logger.py sample"
```

`pythonw.exe` 可以不弹控制台窗口。想常驻一个长期进程的话，改成 `/sc onlogon` 配 `run`。

## 历史文件

```json
{
  "version": 1,
  "days": {
    "2026-10-11": { "download": 2073481, "upload": 134682 }
  },
  "last": { "ts": 1791718172, "up": 4398, "down": 46736 }
}
```

写盘用的是临时文件替换，中途中断不会损坏已有历史。`last` 存的是上一次的计数读数，想强制重新取基线就把这块删掉。
