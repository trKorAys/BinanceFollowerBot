import subprocess
from pathlib import Path
from threading import Thread
import os
import dateparser

try:
    import tkinter as tk
    from tkinter import messagebox
except Exception:  # pragma: no cover - optional on headless systems
    tk = None
    messagebox = None

DATA_FILE = Path(dateparser.__file__).parent / "data" / "dateparser_tz_cache.pkl"
ADD_DATA_ARG = f"{DATA_FILE}{os.pathsep}dateparser/data"

BASE_CMD = [
    "pyinstaller",
    "--onefile",
    "--clean",
    "--name",
    "--add-data",
    ADD_DATA_ARG,
]


def build(target: Path, name: str, status_cb=None) -> None:
    """PyInstaller'i çağırarak tek dosyalık exe üret."""
    cmd = BASE_CMD + [name, str(target)]
    try:
        subprocess.check_call(cmd)
        if status_cb:
            status_cb(f"{name}.exe oluşturuldu")
    except subprocess.CalledProcessError as exc:
        if status_cb:
            status_cb(f"Hata: {exc}")
        else:
            raise


if __name__ == "__main__":
    root_path = Path(__file__).resolve().parent

    if tk is None:
        # GUI kullanılamıyorsa komut satırından çalıştır
        build(root_path / "bot" / "mainnet_bot.py", "mainnet_bot")
        build(root_path / "bot" / "testnet_bot.py", "testnet_bot")
    else:
        def run_build(name):
            def status(msg):
                status_var.set(msg)

            target = root_path / "bot" / f"{name}.py"
            Thread(target=build, args=(target, name, status), daemon=True).start()

        window = tk.Tk()
        window.title("EXE Oluşturucu")

        tk.Label(window, text="Hangi bot için exe oluşturulsun?").pack(pady=5)
        frame = tk.Frame(window)
        frame.pack(pady=5)

        tk.Button(frame, text="mainnet", command=lambda: run_build("mainnet_bot")).pack(side=tk.LEFT, padx=5)
        tk.Button(frame, text="testnet", command=lambda: run_build("testnet_bot")).pack(side=tk.LEFT, padx=5)
        tk.Button(frame, text="her ikisi", command=lambda: [run_build("mainnet_bot"), run_build("testnet_bot")]).pack(side=tk.LEFT, padx=5)

        status_var = tk.StringVar(value="Hazır")
        tk.Label(window, textvariable=status_var).pack(pady=5)

        window.mainloop()
