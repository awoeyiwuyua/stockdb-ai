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
| 无线网卡 | **MT7921U**（MediaTek 0e8d:7961，USB dongle），驱动 `mt7921u`。**2026-10-20 已换到 USB 口 `4-1`**（`xhci-hcd.7.auto/usb4`，另一条 USB 3.0 控制器），`option path` 已同步更新 |
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

### 步骤 0（2026-10-20 第四次排查定稿）：**这是硬件问题，软件无法根治**

> ⚠️ 本节先后被误判过两次（autosuspend → 已证伪；HE80/HE40 → 亦已证伪），
> 详见 §8.3。**最终结论见 §8.1：USB 物理层故障，网卡反复脱落。**

**先做软件侧能做的（减轻症状，不能根治）**：

```sh
# 1) 5G 降到 40MHz（减轻固件 tx 压力，单独用无效但配合硬件修复有好处）
cp /etc/config/wireless /etc/config/wireless.bak-$(date +%Y%m%d-%H%M)
uci set wireless.radio1.htmode='HE40'
uci commit wireless

# 2) 禁用 USB autosuspend（避免无谓挂起/恢复开销）
echo on > /sys/bus/usb/devices/2-1/power/control
echo 0  > /sys/bus/usb/devices/2-1/power/autosuspend_delay_ms
echo -1 > /sys/module/usbcore/parameters/autosuspend
```

**但真正要做的是硬件动作**（见 §8.6）：换 USB 口 / 换网卡 / 换供电。
**软件侧只能保证「掉线后能自动恢复」，不能保证「不掉线」。**

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

## 七个容易踩的坑

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

5. **每次 disconnect 前必有 `timed out waiting for pending tx`**——但这是**症状不是
   原因**（§8.3 假设二）。**判别物理层问题的决定性前兆是 `reset SuperSpeed`
   周期出现**——那才是 USB 链路不稳的信号（§8.1）。

6. **`reset SuperSpeed` 周期出现 = USB 链路不稳**（供电/接触/信号质量），
   **不是驱动问题**。本次先后误判成 autosuspend 与 HE80 固件，**两次都是因为
   没往下追到「设备最终从 USB 总线消失」这个事实**。

7. **探测工具本身可能不可靠——别把「探测失败」当「服务挂了」**。本次两次踩坑：
   - **busybox ash 不支持 `/dev/tcp`** → `(echo > /dev/tcp/HOST/PORT)` 恒失败，
     被我误读成「NAS 端口全 closed」；**实际用 `curl` 探是 401/400（服务正常）**；
   - **`Test-NetConnection` 报 22 端口通，但实际连不上**（中间设备响应了 TCP 探测），
     被我误读成「Tailscale 通道正常」；**实际本机根本没装 Tailscale**。
   → **判据**：端口探测只作参考，**服务是否活着看应用层响应码**
   （401/400 = 活着；连接被重置 = 真有问题）。

## 8. 复发模式与根因（2026-10-20 四次排查定稿）

### 8.1 最终结论：内核 bug 触发的驱动崩溃，网卡随之脱落

**根因 = 内核 bug**（§8.2 有完整 Call Trace）：
`ieee80211_get_vht_max_nss()` 在**停止 AP** 的 STA 速率计算路径上空指针崩溃
→ `mt76_unregister_device` **主动注销驱动** → 网卡从 USB 总线脱落。

**dmesg 完整模式**（旧口 `2-1`，每次循环都一样）：

```
[   17.907453] usb 2-1: reset SuperSpeed USB device number 2   ← 上电
[ 3337.004278] usb 2-1: USB disconnect, device number 2       ← 脱落
[ 3340.340657] usb 2-1: new SuperSpeed USB device number 3
[ 3505.081197] usb 2-1: reset SuperSpeed USB device number 3   ← 165 秒后又被 reset
[ 3766.559402] usb 2-1: USB disconnect, device number 3       ← 再脱落
[ 3770.534028] usb 2-1: new SuperSpeed USB device number 4
[ 4153.914584] usb 2-1: reset SuperSpeed USB device number 4   ← 384 秒后又被 reset
[ 4373.214142] usb 2-1: USB disconnect, device number 4       ← 再脱落
```

**决定性证据**：
- `reset SuperSpeed` → **165~384 秒后** → `USB disconnect` → 重新枚举，**循环往复**；
- 最终 `2-1` 设备路径**整个消失**（`/sys/bus/usb/devices/2-1` 不存在），
  `mt7921u` 驱动下**无设备绑定** → **网卡已物理脱落，不在 USB 总线上**；
- `br-proxyap` 的 MAC 从 `90:de:80:88:71:09` 变成 `9e:2b:ee:3e:07:0e`
  → 桥接口现在绑的不是无线网卡（是残留）；
- **无 over-current / not enough power 记录**；**无 xhci 控制器错误**。

**换口后的实测（2026-10-20，最终）**：换到 `4-1`（另一条 USB 3.0 控制器
`xhci-hcd.7.auto/usb4`）**没有根治**——

```
[   17.487] usb 4-1: reset SuperSpeed USB device number 2   ← 上电
[  510.351] usb 4-1: reset SuperSpeed USB device number 2   ← 8.5 分钟后
[ 1177.189] usb 4-1: USB disconnect, device number 2        ← 19.6 分钟后脱落
[ 1190.185] usb 4-1: new SuperSpeed USB device number 3
[ 1201.606] usb 4-1: USB disconnect, device number 3        ← 11 秒后又掉
[ 1202.014] usb 4-1: new SuperSpeed USB device number 4
```

**两条不同的 USB 3.0 控制器复现同一模式** → 与单个 USB 口的供电无关。

**恢复**：见 §8.3 的三层自愈机制（看门狗 + 端口自动适配 + autosuspend 禁用），
**已装并验证**。

**根治**：§8.7 的硬件动作，**其中优先级最高的是给 NAS 接网线**
（对本项目：直接消除生产环境的 WiFi 单点——与本卡的根因无关但同样解决问题）。

### 8.2 真正的崩溃点（内核 bug，2026-10-20 定位到 Call Trace）

**完整崩溃栈**（`dmesg`，`WARNING: CPU: 0 PID: 56`）：

```
WARNING: CPU: 0 PID: 56 at ../mac80211-regular/backports-6.18.26/net/wireless/util.c:1521
                ieee80211_get_vht_max_nss+0x448/0x49c [cfg80211]
  ieee80211_get_vht_max_nss
  cfg80211_calculate_bitrate
  nl80211_put_sta_rate              ← 设置 STA 速率
  cfg80211_del_sta_sinfo
  sta_set_sinfo
  __sta_info_flush
  ieee80211_nan_func_terminated
  cfg80211_stop_ap                 ← 停止 AP
  cfg80211_register_netdevice
  call_netdevice_notifiers_info
  __dev_close_many
  cfg80211_shutdown_all_interfaces
  ieee80211_unregister_hw
  mt76_unregister_device          ← 驱动被注销 → 网卡脱落
```

**这是内核 bug，不是配置问题**：
- `ieee80211_get_vht_max_nss()` 在 **停止 AP** 时计算 STA 速率路径上解引用空指针；
- 崩溃 → `mt76_unregister_device` → **驱动主动注销设备** → 网卡从 USB 总线脱落
  （所以是 `mt76_unregister_device` 而非 `usb_hub` 踢人）；
- **已知问题**：MT7921U 在 OpenWrt 6.12 上有大量同类崩溃报告
  （`sta_set_sinfo` / `ieee80211_get_vht_max_nss` 路径，社区已知）；
- 内核版本 6.12.94 + `backports.git 6ecbb44` 的 mac80211 回移。

**与 `minstrel_vht_only = Y` 有关**（该内核参数强制速率算法只走 VHT 路径）。
**注意**：该参数**只读**，运行时改不了（`echo 0 > .../minstrel_vht_only` 无效），
只能靠内核升级——本机不打算为此升级内核。

**触发条件**：需要 **有客户端关联**（`sta_set_sinfo` 要处理 STA），
所以「AP 空载时不会崩，有设备连着才崩」——这也解释了为什么掉线间隔
（约 510 秒）与客户端活动相关。

### 8.3 已装的三层自愈机制（2026-10-20）

内核 bug 无法在当前内核上修复，但可以让它**发生后自动恢复**：

| 层 | 位置 | 作用 |
|---|---|---|
| **看门狗** | `/etc/wifi-watchdog.sh` + crontab（每分钟） | 检测到 AP 接口消失或 carrier=0 → unbind/bind 驱动 + 重启 netifd → 恢复 AP（实测约 60 秒含客户端重连） |
| **端口自动适配** | `/etc/hotplug.d/11-wifi-path-detect` | 网卡插入/换口时**自动更新 `radio1.path`** 并 reload 无线——不必再手工改路径（换口是本次踩坑的主因之一） |
| **autosuspend 禁用** | `/etc/hotplug.d/10-usb-nosuspend` + `/etc/rc.local` | 按 **vid:pid**（`0e8d:7961`）匹配设备，禁用 USB 自动挂起（设备路径随端口变化，不能硬编码） |

**看门狗逻辑**（幂等，AP 正常时立即退出）：

```sh
AP=$(ls /sys/class/net/ | grep -E '^phy[0-9]+-ap[0-9]+$' | head -1)
[ -n "$AP" ] && [ "$(cat /sys/class/net/$AP/carrier)" = "1" ] && exit 0   # 正常
# 否则：unbind/bind mt7921u → 清 hostapd 残留 → 重启 netifd
```

**这三层只治标不治本**——根因是内核 bug + USB 物理层不稳。
**根治仍是 §8.7 的硬件动作。**

### 8.4 两次被证伪的假设（记录以免重蹈）

> **这是本篇最有价值的部分**——两次误判的共同教训：
> **每个假设都必须用「改完是否还复现」验证，不能靠「改了之后看起来好了」下结论。**

#### 假设一：USB autosuspend（0.12.8）

当时观察到 `usbcore.autosuspend=2` + 设备 `autosuspend_delay_ms=2000`（2 秒即挂起），
且 MT7921U 在 autosuspend 下掉线是**已知问题** → 禁用之。

**结果：仍然掉线**（禁用后 429 秒再次 disconnect）→ **证伪**。

**误判原因**：autosuspend 与掉线**同时存在**，但**不是因果**。真正的因果链是
「USB 链路不稳 → 控制器 reset → 驱动放弃 → 设备脱落」。

#### 假设二：HE80 固件 tx timeout（0.12.9）

`mt7921u: timed out waiting for pending tx` 确实**每次 disconnect 前都出现**，
且 MT7921U 在 80MHz 下固件易卡 tx 队列（固件 `Build Time: 20231109190959`）→ 降 40MHz。

**结果：仍然掉线**（40MHz 下继续 reset + disconnect）→ **部分证伪**。

**修正**：tx timeout 是**症状不是原因**——USB 链路 reset 时驱动自然等不到 tx 完成。
降 40MHz 本身无害（带宽换稳定），保留。

**教训**：**前兆 ≠ 原因**。`tx timeout` 与 `disconnect` 的时序关系只能说明
「驱动在最后时刻放弃了」，不能说明「带宽导致了放弃」。

### 8.5 已排除的因素

| 因素 | 实测 | 结论 |
|---|---|---|
| 物理拔插 | 每次 disconnect 前都有 `reset SuperSpeed`（非人为） | ❌ 排除 |
| USB autosuspend | 禁用后仍掉线 | ❌ **证伪** |
| HE80 固件 | 降 40MHz 后仍掉线 | ❌ **证伪**（tx timeout 是症状） |
| 供电过流告警 | dmesg 无 over-current / not enough power | ❌ 排除（但供电不足仍是主嫌） |
| 内存不足 | 3G 可用（总 4G） | ❌ 排除 |
| 过热 | CPU 58°C / GPU 54°C | ❌ 排除 |
| xhci 控制器错误 | dmesg 无 xhci error/timeout/stall | ❌ 排除 |
| 定时任务碰 USB | crontab 只有 tailscale / geo-update | ❌ 排除 |
| USB 链路物理不稳 | **`reset SuperSpeed` 周期出现 → 设备最终从总线消失** | ⚠️ **主嫌** |

### 8.6 硬件信息（供后续判断）

- 软路由：**Lunzn FastRhino R68S**（国产 ARM，aarch64）
- 网卡：MT7921U（0e8d:7961）——**2026-10-20 从 `2-1`（`xhci-hcd.6.auto/usb2`）换到
  `4-1`（`xhci-hcd.7.auto/usb4`）**，两条都是 USB 3.0 SuperSpeed 控制器
- 驱动：`mt7921u.ko`（内核 6.12.94）
- **只有一个射频**（radio1 = 5G，**无 2.4G**）→ 5G 挂了就没有备选频段
  （注：`wifi config` 误探测出的 radio0/6G 已删除，见 §9.2）

### 8.7 若要根治

见 §8.7 的四条。**但注意实测结论（2026-10-20）**：**换 USB 口没根治**——
旧口 `2-1` 与新口 `4-1`（两条不同的 USB 3.0 控制器）复现同一模式，
说明不是单个口的供电问题，而是**网卡本身 / 该 USB 控制器链路 / 供电总路**。

按实际有效性排序：

1. **给 NAS 接网线**（`br-lan` 侧）——**对本项目优先级最高**：
   直接消除生产环境的 WiFi 单点，与本卡的根因无关但同样解决问题；
2. **换网卡**（MT7921U 在 OpenWrt 6.12 上不稳 → RTL8812AU / MT7612U）；
3. **带独立供电的 USB 集线器**（绕开板载 USB 供电）；
4. 换 USB 口（**实测无效**——两个口都复现）。

**注意**：本设备**只有 5G**，没有 2.4G 备选——所以一旦 5G 掉线，
5G 网段（`192.168.50.0/24`，NAS 在此）**整体不可达**，
只能走 Tailscale（`100.66.1.5`）。

## 9. 网卡换 USB 口后（2026-10-20 实操，五个坑）

把网卡从 `2-1` 换到 `4-1` 后，**配置不会自动跟上**——OpenWrt 的
`option path` 是硬编码路径。以下五处都要改/修，漏一处就起不来：

### 9.1 `radio1.path` 要改成新路径

```sh
# 查新路径（注意：要用 phy 的 device 路径，不是网卡的）
readlink -f /sys/class/ieee80211/phy0/device
# -> /sys/devices/platform/fd000000.usb/xhci-hcd.7.auto/usb4/4-1/4-1:1.0
# 去掉前导 / 后就是 uci 的 path 值
uci set wireless.radio1.path='platform/fd000000.usb/xhci-hcd.7.auto/usb4/4-1/4-1:1.0'
uci commit wireless
```

> ⚠️ **path 值不能带前导 `/`**——带了就匹配不上（netifd 静默不认）。
> 本次就踩了这个：写成 `/platform/...` 时 AP 一直起不来。

### 9.2 自动探测生成的 `radio0` 会抢占同一个 path

`wifi config`（或某些自动探测流程）会生成一个 `radio0`（`band=6g`、
**`country=00`**），它的 `path` 与 radio1 相同。后果：

```
hostapd: Line 7: Invalid country_code '00'
hostapd: Cannot enable IEEE 802.11d without setting the country_code
hostapd: 2 errors found in configuration file
hostapd: hostapd.add_iface failed for phy phy0
```

**处置**：删掉它（它本机没有 6G 射频，是误探测产物）：

```sh
uci delete wireless.radio0
uci delete wireless.default_radio0
uci commit wireless
```

### 9.3 `rc.local` 的追加内容必须在 `exit 0` 之前

OpenWrt 的 `/etc/rc.local` 模板结尾自带 `exit 0`。**追加在它之后的内容永远
不会执行**——本次的 autosuspend 持久化就是这样静默失效的。

```sh
# ❌ 错：追加在 exit 0 之后
...原有内容...
exit 0
echo 0 > /sys/.../autosuspend_delay_ms     # 永不执行

# ✅ 对：重写整个文件，把追加内容放在 exit 0 之前
```

### 9.4 `hotplug.d` 脚本不能硬编码端口号

设备路径随 USB 口变化（`2-1:1.0` → `4-1:1.0`）。**按 vid:pid 匹配**：

```sh
VID=$(cat "/sys$DEVPATH/idVendor" 2>/dev/null)
PID=$(cat "/sys$DEVPATH/idProduct" 2>/dev/null)
if [ "$VID" != "0e8d" ] || [ "$PID" != "7961" ]; then exit 0; fi
```

（MT7921U = `0e8d:7961`）

### 9.5 `/etc/hotplug.d` 目录可能不存在

被清理过的系统上该目录会消失，`cp` 直接报 `No such file or directory`。
**先 `mkdir -p /etc/hotplug.d`**。

### 9.6 换口后必须重启设备

**旧设备的 `page_pool` 卡死会污染新设备的初始化**——实测换口后
`phy5-ap0` 存在但 hostapd 仍报 694 次 beacon 错误，直到重启才清零。
**rmmod/modprobe 清不掉，只有重启。**

```sh
# ⚠️ 必须用 setsid 脱离 SSH 会话，否则会话结束时进程被杀、重启不发生
setsid sh -c 'sleep 2; reboot' >/dev/null 2>&1 &
```

> **踩过的坑**：先用了 `(sleep 2; reboot) &`，结果 **uptime 没变**（设备根本没
> 重启），却以为重启过了。**验证重启是否真的发生，看 `uptime` 和
> `/proc/sys/kernel/random/boot_id`**，别看 dmesg（内核 ring buffer 会保留）。

### 9.7 换口后的完整操作顺序

```sh
# 1. 改 path（去前导 /）
uci set wireless.radio1.path='<新路径>'
uci commit wireless
# 2. 删误探测的 radio0
uci delete wireless.radio0; uci delete wireless.default_radio0; uci commit wireless
# 3. 重写 rc.local（追加内容在 exit 0 之前，按 vid:pid 匹配）
# 4. 重写 /etc/hotplug.d/10-usb-nosuspend（mkdir -p 目录，按 vid:pid 匹配）
# 5. 重启设备（setsid 脱离会话）
setsid sh -c 'sleep 2; reboot' >/dev/null 2>&1 &
```

**换口后的验证清单**：

```sh
cat /proc/uptime                                  # uptime 应很小
cat /proc/sys/kernel/random/boot_id               # 应与重启前不同
dmesg | grep -c page_pool_release_retry           # 应为 0
logread | grep -c 'Failed to set beacon'          # 应为 0
dmesg | grep -c 'USB disconnect'                 # 应为 0
ip -br link show | grep proxyap                   # 应为 UP
iw dev phy0-ap0 info | grep -E 'channel|width'    # 信道/带宽须与配置一致
iw dev phy0-ap0 station dump | grep -c '^Station' # 关联客户端数 > 0
```

**本次换口后的最终状态**：`page_pool` 0 条、beacon 错误 0 条、USB disconnect 0 次、
`br-proxyap` UP、carrier=1、`channel 36 / width 40 MHz / center1 5190 MHz`、
3 台客户端关联、NAS 的 22/7899/8081 全部恢复。

## 预防

- **给 NAS 接网线**——**对本项目最重要的一条**：生产环境不应依赖 WiFi 单点
  （见 `docs/ops/runbook.md` §3.13）
- **换 USB 口 / 换带独立供电的集线器**——`reset SuperSpeed` 周期出现是主嫌
- 软件侧缓解措施（保留）：`htmode='HE40'` + 禁用 USB autosuspend
- **掉线自动恢复**：本手册 §5 步骤 1~2 可在掉线后恢复 AP（**但修不了掉线本身**）
- 别频繁插拔 USB 无线网卡——oops 就发生在拔插瞬间
  （`ieee80211_get_vht_max_nss` 空指针）
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
