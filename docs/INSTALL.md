# 安装、更新与维护

## 三种部署途径

| 方式 | 是否需要 HACS | 更新方式 |
| --- | --- | --- |
| HACS 自定义仓库 | 是 | HACS 下载版本，重启 HA |
| Release ZIP | 否 | 备份原组件目录，再用新组件目录替换，重启 HA |
| 下载源码后复制组件 | 否 | 选择固定 tag，复制 `custom_components/yutai_ble`，重启 HA |

三种方式最终安装的是同一个 HA 集成。不要给同一个组件目录混用多个更新工具。
不是 Docker 应用、不是 Supervisor 加载项；不能直接粘贴到加载项商店安装。

## 蓝牙准备

HA OS 按 HA 官方方式配置本地蓝牙硬件。HA Container 需由宿主机提供 BlueZ，
通常将 `/run/dbus:/run/dbus:ro` 挂载入容器，并按 HA Bluetooth 要求配置权限。
已有正常运行的 HA 不应为了安装本集成盲目重建容器或覆盖 Compose。

目前没有适配器选择器：若同一地址同时出现在多个 BlueZ 适配器中，会拒绝模糊目标。
先用单个本地适配器验证；蓝牙代理不受支持。适配器需要能协商至少 69 字节 ATT MTU。

## 实体与界面

配置成功后，计量版最多提供 15 个实体，包括总开关、电压/电流/功率、七项设置、输出状态及诊断。
普通版最多提供 7 个实体：总开关、指示灯、通电默认状态、输出状态和三个诊断实体。
未知的配置值不会强行解释为已验证模式，相关设置可能暂不显示。
采用 HA 原生设备页和中文翻译，不会覆盖现有仪表盘，也不会自动复制作者的个人仪表盘。
可以从设备页把总开关和三个计量传感器添加到自己的仪表盘。

“通电默认状态”是下次上电时的行为，不等于当前总开关状态。
双击倒计时的时长是配置值，不是正在运行的剩余时间。
本包不包含本地定时或循环定时，不能沿用其他型号的属性映射。

## 更新和回滚

先做 HA 备份；手动安装者另外保留当前 `custom_components/yutai_ble` 目录。

### 通过 HACS 更新

1. 打开 **HACS → Yutai BLE → ⋮ → 更新信息（Update information）**。
2. 再选 **⋮ → 重新下载（Redownload）**，确认需要安装的版本，例如 `v0.6.1`。
   需要手动选版本时，展开 **Need a different version?**。
   如果 **设置 → 更新** 已出现 Yutai BLE 更新，也可以在那里选择 **安装**。
3. 下载完成后进入 **设置 → 系统 → 右上角电源图标 → 重启 Home Assistant → 重启**。

“更新信息”只刷新版本信息；“重新下载 / 安装”才会写入新代码。
集成代码更新需要重启 Home Assistant，刷新浏览器或仅重新加载集成不能替代这个步骤。
参见 [HACS 官方更新说明](https://www.hacs.xyz/docs/use/update/)、
[仓库操作说明](https://www.hacs.xyz/docs/use/repositories/dashboard/) 和
[HA 官方重启步骤](https://www.home-assistant.io/common-tasks/general/#restarting-home-assistant)。

如果 HA 界面已经使用 **Marketplace** 管理社区集成，进入
**设置 → 更新 → Yutai BLE → 安装**，然后重启 HA。
仓库详情位于 **设置 → Marketplace**；可通过 **⋮ → Install another version** 选择版本。
参见 [HA 官方 Marketplace 文档](https://www.home-assistant.io/integrations/marketplace/)。

### 手动安装者更新

1. 从 [Releases](https://github.com/jsdfhasuh/ha-yutai-ble/releases) 下载组件包，
   例如 **`yutai-ble-v0.6.1.zip`**。
2. 解压后，用包内的 `custom_components/yutai_ble` 替换 HA 配置目录中的同名目录。
   最终路径应为 `/config/custom_components/yutai_ble/manifest.json`，不要多套一层目录。
3. 按上面的路径重启 Home Assistant。

保留 `.storage`、已有集成条目和设备。扫码工具包 `yutai-ble-qr-windows-v版本号.zip`
用于在 Windows 上获取密钥，不是 HA 组件；已有设备无需重新扫码。

### 更新后检查和回滚

重启后确认已安装版本、设备实体正常加载，以及受支持的状态和计量数据继续刷新。
手动安装者可查看 `custom_components/yutai_ble/manifest.json` 中的 `version`。
遇到回归可恢复更新前的 HA 备份；HACS 安装者也可重新下载之前的兼容版本，
手动安装者可恢复保留的旧组件目录，然后再次重启 HA。

0.6.1 保留原有地址生成的实体唯一 ID 和配置结构，不需要主动重建已有条目。
使用 0.4.0 或更早版本者需先更新组件、重启 HA，才会看到普通版型号选项。
发布新版本不会自动更新现存部署；升级后的实体和自动化仍需核对。

## 常见问题

**找不到集成：** 确认路径是 `custom_components/yutai_ble/manifest.json`，安装后重启 HA。
在浏览器刷新页面；HACS 下载成功不等于已添加设备。

**添加失败或反复断连：** 确认插座及上级排插有电，关闭占用连接的手机蓝牙，
缩短距离，检查 HA 的 Bluetooth 集成和 D-Bus 挂载。恢复后等待自动重试，
不要并行运行米家控制页、独立蓝牙探测脚本和本集成。

**有功率，为什么没有用电量：** 本包只提供即时 W，不把未知计数器当作 kWh。
可另用 HA 的积分传感器把功率转换成估算电量，再配合 Utility Meter 分周期统计。
断连和采样间隔会影响精度；这不是插座内部的累计电量，也不会还原米家历史记录。

**普通版和炎米插座能否添加：** 0.5.0 支持普通版 fsov7m 和计量版 fsov8m。
炎米 Y1 仍仅提供仓库 `research` 里的独立测试源码，尚未实现 HA 配置入口。
不要把研究目录直接复制进 `custom_components`；详见 [型号与验证范围](MODELS.md)。

**密钥会过期吗：** 重置、解绑或重新配对可能改变密钥；用设备所有者账号重新扫码获取。
工具暂只查询中国大陆区的自有家庭，未覆盖共享家庭、其他地区和所有分页场景。

## 验收边界

离线测试覆盖序列化、鉴权结果检查、读写回执、取消清理、退避以及多设备身份隔离。
它不能替代真实射频环境、多个同型号实物、固件差异、HACS 客户端安装和继电器动作验收。
首次实际开关测试应使用空载或可安全断电的负载，由用户自行确认。
