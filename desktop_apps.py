"""Windows Store desktop apps, identified by package identity, never image name alone."""
import os
import time
import xml.etree.ElementTree as ET
from functools import lru_cache
from pathlib import Path


PACKAGE_NAMES = {
    "codex": {"OpenAI.Codex"},
    "chatgpt": {"OpenAI.ChatGPT-Desktop", "OpenAI.ChatGPT"},
}
REPOSITORY = r"Software\Classes\Local Settings\Software\Microsoft\Windows\CurrentVersion\AppModel\Repository\Packages"
PUBLISHER_ID = "2p2nqsd0c76g0"


def inside(path, root):
    try:
        return Path(path).resolve().is_relative_to(Path(root).resolve())
    except (ValueError, OSError):
        return False


def read_package(full_name, root, kind):
    parts = full_name.split("_")
    if len(parts) != 5 or parts[0] not in PACKAGE_NAMES.get(kind, set()) or parts[-1] != PUBLISHER_ID:
        return None
    manifest = ET.parse(Path(root) / "AppxManifest.xml").getroot()
    identity = manifest.find("{*}Identity")
    if identity is None or identity.get("Name") != parts[0]:
        return None
    for application in manifest.findall("{*}Applications/{*}Application"):
        executable = Path(root) / application.get("Executable", "")
        app_id = application.get("Id", "")
        if not app_id or not inside(executable, root) or not executable.is_file():
            continue
        return {"root": str(Path(root).resolve()), "executable": str(executable.resolve()),
                "aumid": f"{parts[0]}_{parts[-1]}!{app_id}", "package": full_name,
                "version": tuple(int(value) for value in parts[1].split("."))}
    return None


@lru_cache(maxsize=8)
def _packages(kind, time_bucket):
    if os.name != "nt":
        return []
    import winreg
    found = []
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REPOSITORY) as repository:
            for index in range(winreg.QueryInfoKey(repository)[0]):
                name = winreg.EnumKey(repository, index)
                if name.split("_")[0] not in PACKAGE_NAMES.get(kind, set()):
                    continue
                try:
                    with winreg.OpenKey(repository, name) as key:
                        root = winreg.QueryValueEx(key, "PackageRootFolder")[0]
                    package = read_package(name, root, kind)
                    if package:
                        found.append(package)
                except (OSError, ValueError, ET.ParseError):
                    continue
    except FileNotFoundError:
        pass
    return sorted(found, key=lambda item: item["version"], reverse=True)


def packages(kind):
    return _packages(kind, int(time.monotonic() // 10))


def processes(kind, psutil):
    """Capture main apps and their package-local children; never unrelated services."""
    installed = packages(kind)
    if not psutil:
        raise RuntimeError("Desktop process inspection requires psutil.")
    executables = {os.path.normcase(item["executable"]): item for item in installed}
    names = {Path(item["executable"]).name.lower() for item in installed}
    found = {}
    for process in psutil.process_iter(["pid", "name"]):
        if (process.info.get("name") or "").lower() not in names:
            continue
        try:
            package = executables.get(os.path.normcase(process.exe()))
            if not package:
                continue
            for target in [process, *process.children(recursive=True)]:
                try:
                    if inside(target.exe(), package["root"]):
                        found[target.pid] = target
                except psutil.NoSuchProcess:
                    continue
        except psutil.NoSuchProcess:
            continue
        except psutil.AccessDenied as exc:
            raise PermissionError("Windows denied desktop process inspection; controls are blocked.") from exc
    return installed, set(found)
