# Windows 米家扫码工具

独立的可选工具。日常 HA 控制不需要它常驻，也不需要手机一直开蓝牙。

## 使用

1. 安装官方 Python 3.11 或更新版本，包含 Tcl/Tk，并安装 Python launcher 或勾选加入 PATH。
2. 解压扫码工具包，双击 `start_qr.cmd`。首次会在当前目录创建 `.venv` 并安装 `aiohttp`，需要联网。
3. 米家 App 扫描浏览器二维码并确认登录，在命令窗口输入设备编号。
4. 记下显示的型号和 MAC；密钥自动加密保存到 `private/<设备地址>/<时间>/target.dpapi`。
5. 双击 `copy_key.cmd`，选择刚保存的文件。确认弹窗中的型号和地址属于要添加的插座，
   点击“复制密钥”，粘贴到 HA 的 `gatt_ltmk` 字段，然后点击“清空并关闭”。

复制后 90 秒自动清空当前剪贴板（仅当内容仍是该密钥）。
**使用前关闭 Windows 剪贴板历史、跨设备同步和第三方剪贴板管理器**；
清空当前剪贴板不能删除它们已经保存的副本。密钥不显示在终端，也不写入明文文件。

工具只查询中国大陆区账号下自有家庭，不保证列出共享设备或超出单页数量的设备。
扫码列表可包含其他蓝牙设备，但本 HA 集成仅支持 `yutai.plug.fsov7m` 和 `yutai.plug.fsov8m`；
`copy_key.cmd` 会拒绝其他型号和 16 字节 bindkey，防止填错。

## 已有加密文件

`copy_key.cmd` 可选择此前同一 Windows 用户生成的 `target.dpapi`，无需重新扫码。
文件使用当前 Windows 用户 DPAPI 保护，拷到他人电脑不能直接解密。
分享项目时不要附带 `private`；其他用户用自己的米家账号重新获取。

## 手动运行

```powershell
py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe simple_qr.py
.venv\Scripts\python.exe copy_key.py
```

命令行不接受明文密钥参数。不要把 HA 配置、令牌、二维码或密钥提交到 Issue。
本工具的界面和 DPAPI 流程需要在 Windows 上实际使用确认；发布测试不读取用户真实凭据。
