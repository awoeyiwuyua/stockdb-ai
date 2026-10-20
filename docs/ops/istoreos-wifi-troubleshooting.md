# iStoreOS WiFi 掉线排障（MT7921U USB 网卡）

> **实测场景**：2026-10-20，5G SSID `Pandora_passwall` 整个消失。
> 根因 = **USB 无线网卡被拔出重插时驱动崩溃**（内核 oops），而非配置问题。
> 本书记录「症状 → 判据 → 处置」，供下次同样症状时直接用。

## 环境速查

| 项 | 值 |
|---|---|
| 设备 | iStoreOS 25.12.5（OpenWrt 6.12.94，aarch64） |
| 管理地址 | `192.168.31.100`（**走有线时**；LAN 侧是 `192.168.50.1`） |
| SSH | `ssh root@192.168.31.100`（密钥 `~/.ssh/id_ed25519` 可直接登录，无需密码） |
| 无线网卡 | **MT7921U**（MediaTek 0e8d:7961，USB dongle），驱动 `mt7921u` |
| 5G AP | `ssid=Pandora_passwall`，`channel=36`，`htmode=HE80`，`sae-mixed` |
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

### 步骤 4：验证（必做，别只看 `ip link`）

```sh
iw dev phy3-ap0 info | grep -E 'ssid|channel|width|center1'   # 信道/带宽须与配置一致
cat /sys/class/net/phy3-ap0/carrier                            # 须为 1
iw dev phy3-ap0 station dump | grep -c '^Station'               # 关联客户端数
ip neigh show dev br-proxyap | grep -v INCOMPLETE               # 客户端 ARP
ping -c 2 -W 2 192.168.50.146                                  # 实测 NAS 通不通
```

**本例最终状态**：`channel 36 (5180 MHz), width: 80 MHz, center1: 5210 MHz`（与配置
一致，beacon 其实已生效）、`carrier=1`、station 4 台、ping NAS **0% 丢包**。

## 三个容易踩的坑

1. **「Failed to set beacon parameters」不代表 beacon 没生效**——本例中驱动实际
   已按 `channel 36 / 80MHz / center1 5210MHz` 正确工作，该错误是重载期间的残留。
   **判据看 `iw dev <phy>-ap0 info` 的实际信道，不要只看 hostapd 日志。**

2. **phy 索引会漂移**——每次 unbind/bind 都 +1。配置用 `option path` 所以不用改，
   但**必须重启 netifd**，否则 hostapd 会一直找旧的 `phy0`。

3. **`page_pool_release_retry() stalled` 清不掉**——rmmod/modprobe 无效，
   **只能重启设备**。它不阻断转发（WiFi 照常工作），但会持续增长，
   且**下次网卡再被插拔时可能再次触发同样的驱动崩溃**。

## 预防

- **别频繁插拔 USB 无线网卡**——本例的 oops 就发生在拔插瞬间
  （`ieee80211_get_vht_max_nss` 空指针）。
- 插拔后若 WiFi 不回来，按本篇处置走一遍。
- 长期看，`page_pool` 卡死累积到一定程度可能影响转发能力；
  建议在方便的时间重启一次设备清干净（5G 网段会断约 1-2 分钟）。

## 附：5G 与有线是两张网

| 网桥 | 网段 | 用途 |
|---|---|---|
| `br-lan` | `192.168.31.0/24` | 有线客户端（NAS 管理口也在此：`192.168.31.1`） |
| `br-proxyap` | `192.168.50.0/24` | **5G 客户端**（NAS 走 WiFi 时是 `192.168.50.146`） |

> **排障提醒**：机器切到有线（`192.168.31.x`）后，NAS 的**局域网 IP 变成
> `192.168.50.146` 不可达**——它挂在 5G 网段上。Tailscale 地址
> `100.66.1.5` 始终可用，是跨网段访问 NAS 的可靠通道。
