"""Copy a supported device key from DPAPI to the clipboard, never stdout."""

import json
from pathlib import Path
import re


def decode_payload(raw):
    data = json.loads(raw)
    if (not isinstance(data, dict) or data.get("model") not in ("yutai.plug.fsov8m", "yutai.plug.fsov7m")
            or not isinstance(data.get("address"), str)
            or re.fullmatch(r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", data["address"]) is None
            or not isinstance(data.get("gatt_ltmk"), str)
            or re.fullmatch(r"[0-9A-Fa-f]{64}", data["gatt_ltmk"]) is None):
        raise ValueError("unsupported_device_or_key")
    return data["address"].upper(), data["model"], data["gatt_ltmk"].lower()


def main():
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
    from cloud_auth import dpapi_unprotect

    root = tk.Tk()
    root.withdraw()
    key = ""
    copied = False

    def clear_and_close():
        nonlocal key
        try:
            if copied and root.clipboard_get() == key:
                root.clipboard_clear()
                root.update()
        except tk.TclError:
            pass
        key = ""
        root.destroy()

    try:
        filename = filedialog.askopenfilename(title="选择 target.dpapi", initialdir=Path(__file__).parent / "private",
                                             filetypes=[("Encrypted device key", "*.dpapi")])
        if not filename:
            return 0
        path = Path(filename)
        if path.stat().st_size > 65536:
            raise ValueError("invalid_file")
        address, model, key = decode_payload(dpapi_unprotect(path.read_bytes()))
        root.title("禹泰蓝牙插座密钥")
        root.geometry("480x240")
        root.resizable(False, False)
        frame = ttk.Frame(root, padding=20)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=f"{model}\n{address}").pack(anchor="w", pady=(0, 12))
        status = tk.StringVar(value="请先关闭剪贴板历史和跨设备同步。\n确认这是要添加的插座，再复制密钥。")
        ttk.Label(frame, textvariable=status, wraplength=440).pack(anchor="w", pady=(0, 12))

        def copy():
            nonlocal copied
            root.clipboard_clear()
            root.clipboard_append(key)
            copied = True
            root.update()
            button.configure(state="disabled")
            status.set("已复制，请粘贴到 HA 的 GATT 长期密钥字段。\n90 秒后清空当前剪贴板并关闭。")
            root.after(90000, clear_and_close)

        button = ttk.Button(frame, text="复制密钥", command=copy)
        button.pack(side="left")
        ttk.Button(frame, text="清空并关闭", command=clear_and_close).pack(side="right")
        root.protocol("WM_DELETE_WINDOW", clear_and_close)
        root.deiconify()
        root.mainloop()
        return 0
    except Exception:
        messagebox.showerror("未完成", "无法读取此文件。请使用创建文件的 Windows 用户，确认型号为 fsov8m 或 fsov7m 且包含 GATT 长期密钥。")
        return 1
    finally:
        try:
            if root.winfo_exists():
                clear_and_close()
        except tk.TclError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
