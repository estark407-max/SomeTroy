"""Версия сборки берётся из одного места — APP_VERSION в troy_desktop.py.
Обновляет version.txt (свойства SomeTroy.exe) и печатает версию для build.bat."""
import re
from pathlib import Path

here = Path(__file__).resolve().parent
ver = re.search(r'^APP_VERSION = "([\d.]+)"', (here / "troy_desktop.py").read_text(encoding="utf-8"), re.M).group(1)
nums = (ver.split(".") + ["0", "0", "0"])[:4]
info = here / "version.txt"
text = info.read_text(encoding="utf-8")
text = re.sub(r"(filevers|prodvers)=\([\d, ]+\)", lambda m: f"{m.group(1)}=({', '.join(nums)})", text)
text = re.sub(r"'(FileVersion|ProductVersion)', '[\d.]+'", lambda m: f"'{m.group(1)}', '{'.'.join(nums)}'", text)
info.write_text(text, encoding="utf-8")
print(ver)
