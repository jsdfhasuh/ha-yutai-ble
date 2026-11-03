# 独立插座研究源码

这里公开此前在两款实物上做过的协议验证代码，不是额外的 HA 集成。
HACS 不会安装或加载这个目录。

- [禹泰普通版 `yutai.plug.fsov7m`](yutai_fsov7m/README.md)
- [炎米 Y1 `wiing.plug.v7icm`](wiing_v7icm/README.md)
- [三款型号完整验证范围](../docs/MODELS.md)

普通版基础控制和计量版的 HA 代码位于 `custom_components/yutai_ble`。
GitHub 的 Source code ZIP 包含三款源码。
Release 中 `yutai-ble-v0.5.0.zip` 仅用于安装 HA 集成，不包含研究脚本。

## 与历史私有实验的差异

保留原来的协议帧、鉴权、属性白名单、计时和写入保护；移除硬编码个人地址。
每次调用使用不可变目标对象，地址和密钥来自该次 JSON stdin，不通过命令行或环境变量传递。
测试中的地址是合成值。原有云端账号、SSH/ADB 运行器、手机抓包、原始测试日志及密钥不公开。
公共说明只总结协议证据，不公开住宅设备布局或当前状态。

## 离线测试

从仓库根目录分别运行，避免两套独立模块同名造成导入冲突：

```sh
python -B -m unittest discover -s research/yutai_fsov7m -v
python -B -m unittest discover -s research/wiing_v7icm -v
```

只需 Python 3.11+ 标准库；测试使用模拟对象及故意无效的命令输入，不启动真实蓝牙。
发布副本的地址配置改造只经过离线测试，没有再次做硬件操作。

## 硬件调用边界

实际调用需要 Linux BlueZ、可用的 D-Bus、`bleak`、`dbus_fast` 和 `cryptography`。
调用者必须自行建立独占访问、密钥保护、超时和设备识别边界。
只运行 `read_probe.py` 为读取；运行控制脚本会改变真实设备，名称不同不代表无副作用。

所有脚本从 stdin 接收一个 JSON 对象。基础字段为 `address`、该目录唯一允许的 `model`、
64 个十六进制字符的 `gatt_ltmk`；读完 stdin 后关闭输入。
不要把包含密钥的 JSON 写进命令历史、源码、日志或公开 Issue。
设置/定时脚本还要求其 `validated_input()` 中列出的明确选项和新鲜期望字，不能猜测。

`cycle_probe.py` 是**空载测试**：先读取当前状态，切到相反状态，再尝试恢复原状态，
最多两次 SET。恢复失败不能保证输出安全，不可用于连接电器的日常开关控制。
这些实验脚本不具备 HA 集成的全局排队和退避机制，不能多进程并发运行。
不得把 fsov7m 的定时/设置字直接套用到 fsov8m 或炎米 Y1。
