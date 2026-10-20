# iStoreOS WiFi 掉线排障（MT7921U USB 网卡）

> **实测场景**：2026-10-20，5G SSID `Pandora_passwall` 整个消失。
> 根因 = **USB 无线网卡被拔出重插时驱动崩溃**（内核 oops），而非配置问题。
> 本书记录「症状 → 判据 → 处置」，供下次同样症状时直接用。

## 环境速查

| 项 | 值 |
|---|---|
| 设备 | iStoreOS 25.12.5（OpenWrt 6.12.94，aarch64），硬件 **Lunzn FastRhino R68S** |
| 管理地址 | `192.168.31.100`（**走有线时**；LAN 侧是 `192.168.50.1`） |
| SSH | `ssh root@192.168.31.100`（密钥 `~/.ssh/id_ed25519` 可直接登录，无需密码） |
| 无线网卡 | **MT7921U**（MediaTek 0e8d:7961，USB dongle），驱动 `mt7921u`，挂 `xhci-hcd.6.auto/usb2`（USB 3.0） |
| USB autosuspend | **已禁用**（`usbcore.autosuspend=-1` + 设备 `autosuspend_delay_ms=0`），持久化在 `/etc/hotplug.d/10-usb-nosuspend` + `/etc/rc.local` |
| 5G AP | `ssid=Pandora_passwall`，`channel=36`，**`htmode=HE40`**（原 HE80，因固件 tx timeout 缺陷降级，见 §8.1），`sae-mixed` |
| AP 网桥 | `br-proxyap` = `192.168.50.1/24`（**5G 客户端在此网段**，NAS 在 `.146`） |
| 有线网桥 | `br-lan` = `192.168.31.100/24` |
| radio 绑定 | `wireless.radio1` 用 **`option path`**（不是写死 phy 号）——这点很关键 |

## 症状与判据

| 看到什么 | 判据命令 | 结论 |
|---|---|---|
| WiFi 全消失，客户端连不上 | `ip -br link show br-proxyap` → **DOWN** | 网卡掉了 |
| `br-proxyap` DOWN 但 eth 正常 | `dmesg \| grep -E 'USB disconnect\|WARNING: CPU'` | **驱动崩溃**（本例） |
| hostapd 每 6 秒刷 "Failed to set beacon parameters" | `logread \| grep beacon` | 网卡已掉，hostapd 反复重试 |
| 5G 客户端拿不到 IP | `ip neigh show dev br-proxyap` 全是 `INCOMPLETE` | AP 未工作 |

## 关键判据（区分「配置错」vs「驱动崩」）

```sh
# 1. 网卡在不在？
ls -1 /sys/class/ieee80211

# 2. 驱动崩过没有？（WARNING: CPU = 内核 oops）
dmesg | grep -E 'USB disconnect|WARNING: CPU|Call Trace' | tail -5

# 3. hostapd 认的是哪个 phy？
ubus call network.wireless status | grep ifname
```

**本次实证的时间线**（dmesg 时间戳，`365xxx` 为崩溃瞬间）：

```
365397.436  usb 2-1: USB disconnect, device number 2      ← 网卡被拔出
365398.244  WARNING: CPU: 0 PID: 12063 ... ieee80211_get_vht_max_nss   ← 内核 oops
365398.255  mt76_unregister_device / mt792xu_disconnect
365401.548  usb 2-1: new SuperSpeed USB device number 3  ← 重新插入
365402.008  mt7921u: WM Firmware Version ...             ← 固件加载成功
365461.564  page_pool_release_retry() stalled pool shutdown: id 7, 9 inflight 60 sec
             ...（此后每 60 秒 +1，持续增长）
```

**要点**：网卡重新插入后**固件是加载成功的**，但 `page_pool_release_retry() stalled`
说明**旧驱动实例的 DMA 内存池没释放干净**——新实例起不来。这是本例的核心障碍。

## 处置（分步，每步都验证）

### 步骤 0（2026-10-20 第三次排查定稿）：5G 降到 40MHz —— **这才是根因对策**

> ⚠️ 本节曾被误判为「根因」过一次（autosuspend 假设，**已被证伪**——见 §8.3）。
> 真正的根因是 **mt76 驱动的 tx timeout（HE80 固件缺陷）**，见 §8.1。

**先降带宽**（治本，优先级最高）：

```sh
cp /etc/config/wireless /etc/config/wireless.bak-$(date +%Y%m%d-%H%M)
uci set wireless.radio1.htmode='HE40'      # 原 HE80
uci commit wireless
wifi down && sleep 3 && wifi up
```

**验证**：`iw dev <phy>-ap0 info | grep width` → 应为 `width: 40 MHz`。

> 代价：5G 峰值速率下降。若 40MHz 仍不稳 → 换 `VHT80`（关 WiFi 6，保留 80MHz）。
> 可选值：`HE80 / HE40 / VHT80 / VHT40 / HT40`

### 步骤 0b（可选，本身有益但**不是**根因）：关掉 USB autosuspend

MT7921U 在 USB autosuspend（自动挂起）下掉线是**已知问题**，禁用它可避免无谓的
挂起/恢复开销——但**实测禁用后仍然掉线**（§8.3），所以别把它当根因。

**判据**：

```sh
cat /sys/bus/usb/devices/2-1/power/autosuspend_delay_ms   # 2000 = 2 秒就挂起
cat /sys/module/usbcore/parameters/autosuspend           # 2 = 启用自动挂起
```

**处置**（可逆）：

```sh
# 设备级 + 接口级
echo on > /sys/bus/usb/devices/2-1/power/control
echo 0  > /sys/bus/usb/devices/2-1/power/autosuspend_delay_ms
# 内核全局
echo -1 > /sys/module/usbcore/parameters/autosuspend
```

**持久化**（否则重启后失效）——两处都写，hotplug 不保证触发：

```sh
# /etc/hotplug.d/10-usb-nosuspend
#!/bin/sh
[ "$ACTION" = "add" ] || exit 0
case "$DEVPATH" in
  */2-1:1.0) ;;
  *) exit 0 ;;
esac
DEV="/sys$DEVPATH"
[ -d "$DEV/power" ] || exit 0
echo on > "$DEV/power/control" 2>/dev/null
echo 0  > "$DEV/power/autosuspend_delay_ms" 2>/dev/null
```

```sh
# /etc/rc.local 追加（兜底）
for d in /sys/bus/usb/devices/2-1 /sys/bus/usb/devices/2-1/1-0:1.0; do
  [ -d "$d/power" ] || continue
  echo on > "$d/power/control" 2>/dev/null
  echo 0  > "$d/power/autosuspend_delay_ms" 2>/dev/null
done
echo 3 > /sys/module/usbcore/parameters/autosuspend 2>/dev/null
```

### 步骤 1：解绑 / 重新绑定 USB 驱动

```sh
echo 2-1:1.0 > /sys/bus/usb/devices/2-1:1.0/driver/unbind   # 解绑
sleep 3
echo 2-1:1.0 > /sys/bus/usb/drivers/mt7921u/bind            # 重绑
```

> ⚠️ **副作用**：重绑后 **phy 索引会变**（phy0 → phy1 → phy2 …）。
> 配置用的是 `option path`（不是 phy 号），所以**配置不用改**，
> 但 netifd 必须重新解析才会跟上。

### 步骤 2：清 hostapd 残留 + 重启网络（关键，别漏）

```sh
rm -f /var/run/hostapd-phy0-ap0.* /var/run/hostapd-phy0.conf /tmp/run/hostapd-phy0.conf
/etc/init.d/network restart
sleep 12
```

> **只做步骤 1 是不够的**——本例中 unbind/bind 后 phy 变成了 `phy2`，
> 而 hostapd 仍在找 `phy0`（`Failed to open phy phy0`），
> 必须重启 netifd 才会按 `path` 重新解析到新 phy。

### 步骤 3（可选）：彻底重载驱动模块

```sh
ubus call network.wireless down '{"device":"radio1"}'
rmmod mt7921u && sleep 5 && modprobe mt7921u
ubus call network.wireless up '{"device":"radio1"}'
```

> 本例中这步**没能清掉 `page_pool` 卡死**（硬件 DMA 缓冲残留，reload 模块无效），
> 但接口能起来、能转发。**真正清干净要重启设备。**

### 步骤 4：重启设备（清 page_pool 卡死）

```sh
# ⚠️ OpenWrt 上 `echo 3 > /proc/sysrq-trigger` **不生效**
#    （kernel.sysrq=1 已开，但触发被拦，uptime 不变——实测踩过）。
#    必须用 reboot 命令：
(sleep 2; reboot) >/dev/null 2>&1 &
```

> 重启会中断 5G 网段所有设备（含 NAS），约 1~2 分钟。
> NAS 侧容器 `restart: unless-stopped` 会自愈。

### 步骤 5：验证（必做，别只看 `ip link`）

```sh
iw dev phy3-ap0 info | grep -E 'ssid|channel|width|center1'   # 信道/带宽须与配置一致
cat /sys/class/net/phy3-ap0/carrier                            # 须为 1
iw dev phy3-ap0 station dump | grep -c '^Station'               # 关联客户端数
ip neigh show dev br-proxyap | grep -v INCOMPLETE               # 客户端 ARP
ping -c 2 -W 2 192.168.50.146                                  # 实测 NAS 通不通
```

**本例最终状态**：`channel 36 (5180 MHz), width: 80 MHz, center1: 5210 MHz`（与配置
一致，beacon 其实已生效）、`carrier=1`、station 4 台、ping NAS **0% 丢包**。

## 四个容易踩的坑

1. **「Failed to set beacon parameters」不代表 beacon 没生效**——本例中驱动实际
   已按 `channel 36 / 80MHz / center1 5210MHz` 正确工作，该错误是重载期间的残留。
   **判据看 `iw dev <phy>-ap0 info` 的实际信道，不要只看 hostapd 日志。**

2. **phy 索引会漂移**——每次 unbind/bind 都 +1。配置用 `option path` 所以不用改，
   但**必须重启 netifd**，否则 hostapd 会一直找旧的 `phy0`。

3. **`page_pool_release_retry() stalled` 清不掉**——rmmod/modprobe 无效，
   **只能重启设备**。它不阻断转发（WiFi 照常工作），但会持续增长，
   且**下次网卡再被插拔时可能再次触发同样的驱动崩溃**。

4. **`echo 3 > /proc/sysrq-trigger` 在 OpenWrt 上不生效**——`kernel.sysrq=1` 已开，
   但触发被拦，**uptime 不变**（会误以为已重启）。用 `reboot` 命令。

5. **每次 disconnect 前必有 `timed out waiting for pending tx`**——这是**判别
   「驱动超时」与「物理拔插」的决定性前兆**（§8.1）。只看到 `USB disconnect`
   就下结论「网卡被拔了」会误判方向——本次就因此先误判成 autosuspend。

## 8. 复发模式与根因（2026-10-20 三次排查定稿）

**第一次修复只清了 `page_pool`，没解决掉线本身**——重启后 **55 分钟**（uptime 3337 秒）
网卡再次无征兆 disconnect，同样的 oops + page_pool 卡死复现。

### 8.1 真正的根因：`mt76` 驱动的 tx timeout（HE80 固件 bug）

**每次掉线前的必然前兆**（dmesg 逐行）：

```
[3339.890569] mt7921u 2-1:1.0: timed out waiting for pending tx   ← 发送队列超时
[3339.897638] br-proxyap: port 1(phy0-ap0) entered disabled state
...
[3766.559402] usb 2-1: USB disconnect, device number 3             ← 驱动放弃，设备重枚举
[3769.413928] mt7921u 2-1:1.0: timed out waiting for pending tx   ← 下一轮重复
```

**掉线间隔**：3337s → 3766s（**相隔 429 秒**）——不是随机，是负载相关的规律。

**判定依据**：
- 固件版本 `Build Time: 20231109190959`（2023-11，mt76 驱动的 tx timeout 是**已知问题**）
- 配置为 `htmode='HE80'`（WiFi 6 80MHz）——**MT7921U 在 80MHz 模式下固件易卡 tx 队列**
- 每次 disconnect 前**必定**有 `timed out waiting for pending tx`（不是物理拔插）

### 8.2 处置：降到 40MHz（已实施）

```sh
cp /etc/config/wireless /etc/config/wireless.bak-$(date +%Y%m%d-%H%M)
uci set wireless.radio1.htmode='HE40'
uci commit wireless
wifi down && sleep 3 && wifi up
```

**实测结果**：`width: 40 MHz, center1: 5190 MHz`（原 `80 MHz / 5210 MHz`），
carrier=1，NAS 恢复（`192.168.50.146:22` 与 `:7899` 均通）。**配置已持久化。**

> 代价：5G 峰值速率下降（80MHz→40MHz）。若 40MHz 仍不稳，下一候选是
> **`VHT80`**（关掉 WiFi 6，保留 80MHz 带宽）——两者只能二选一。
> 可选值：`HE80 / HE40 / VHT80 / VHT40 / HT40`

### 8.3 被证伪的假设：USB autosuspend

> ⚠️ **这一条曾经被误判为根因，记录在此以免重蹈。**

当时观察到 `usbcore.autosuspend=2` + 设备 `autosuspend_delay_ms=2000`（2 秒即挂起），
且 MT7921U 在 autosuspend 下掉线是**已知问题**——于是禁用之（设备级 `delay=0` +
`usbcore autosuspend=-1`，并用 `/etc/hotplug.d/10-usb-nosuspend` + `/etc/rc.local`
双写持久化）。

**结果：仍然掉线**（禁用后 429 秒再次 disconnect）→ **假设被证伪**。

**但禁用 autosuspend 仍保留**——它对该设备有益（避免无谓的挂起/恢复开销），
只是**不是掉线的原因**。

**教训**：**相关性不是因果**。`autosuspend` 与掉线同时存在，但它不是原因；
真正的因果链是「HE80 固件 → tx 队列超时 → 驱动放弃 → 设备重枚举」。
若当时先验证「禁用后是否还掉」，就能省掉一轮误判。

### 8.4 其他已排除的因素

| 因素 | 实测 | 结论 |
|---|---|---|
| 供电不足 | `bMaxPower=160mA`，dmesg 无 over-current / not enough power | ❌ 排除 |
| 内存不足 | 3G 可用（总 4G） | ❌ 排除 |
| 过热 | CPU 58°C / GPU 54°C | ❌ 排除 |
| xhci 控制器错误 | dmesg 无 xhci error/timeout/stall | ❌ 排除 |
| 定时任务碰 USB | crontab 只有 tailscale / geo-update | ❌ 排除 |
| 物理拔插 | 每次 disconnect 前都有 `timed out waiting for pending tx` | ❌ 排除 |
| USB autosuspend | 禁用后仍掉线 | ❌ **证伪** |

### 8.5 硬件信息（供后续判断）

- 软路由：**Lunzn FastRhino R68S**（国产 ARM，aarch64）
- 网卡：MT7921U（0e8d:7961）挂在 `xhci-hcd.6.auto/usb2`（USB 3.0 SuperSpeed）
- 驱动：`mt7921u.ko`（内核 6.12.94）

### 8.6 若 40MHz 仍不稳，按此顺序查

1. **换 `htmode`** → `VHT80`（保留带宽，关 WiFi 6）
2. **换网卡** → RTL8812AU / MT7612U（避开 mt76 系列的 tx timeout 缺陷）
3. **换 USB 口** → 避开 USB 3.0（供 500mA），或用带独立供电的 USB 集线器
4. **检查接头接触** → 「无征兆 disconnect」的常见原因

**长期解法**：给 NAS 接**网线**（`br-lan` 侧）——让生产环境不再依赖 WiFi 单点
（这条对**本项目**尤其重要，见 `docs/ops/runbook.md` §3.13）。

## 预防

- **5G 用 40MHz（`htmode='HE40'`）**——这是本设备掉线的**真根因对策**
- **禁用 USB autosuspend**（见步骤 0）——本身有益，但不是根因
- **别频繁插拔 USB 无线网卡**——oops 就发生在拔插瞬间
  （`ieee80211_get_vht_max_nss` 空指针）
- 掉线后按本篇处置走一遍（步骤 0 → 1 → 2 → 5）
- 长期：`page_pool` 卡死累积到一定程度可能影响转发能力；
  建议在方便的时间重启一次设备清干净（5G 网段会断约 1-2 分钟）

## 附：5G 与有线是两张网

| 网桥 | 网段 | 用途 |
|---|---|---|
| `br-lan` | `192.168.31.0/24` | 有线客户端（NAS 管理口也在此：`192.168.31.1`） |
| `br-proxyap` | `192.168.50.0/24` | **5G 客户端**（NAS 走 WiFi 时是 `192.168.50.146`） |

> **排障提醒**：机器切到有线（`192.168.31.x`）后，NAS 的**局域网 IP 变成
> `192.168.50.146` 不可达**——它挂在 5G 网段上。Tailscale 地址
> `100.66.1.5` 始终可用，是跨网段访问 NAS 的可靠通道。
