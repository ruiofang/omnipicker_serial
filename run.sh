#!/usr/bin/env bash
set -euo pipefail
tool_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# Check from the application's directory so local module lookup matches launch.
cd -- "$tool_dir"
dependency_check=$(cat <<'PY'
import sys
try:
    if sys.version_info < (3, 10):
        raise RuntimeError("本工具需要 Python 3.10 或更新版本")
    from PyQt5 import QtCore, QtWidgets
    import serial
    missing = [name for name in (
        "Serial", "SerialException", "SerialTimeoutException", "EIGHTBITS",
        "PARITY_NONE", "STOPBITS_ONE") if not hasattr(serial, name)]
    if missing:
        raise RuntimeError("serial 模块不是完整的 pyserial：缺少 " + ", ".join(missing)
                           + "；模块路径：" + str(getattr(serial, "__file__", None)))
    if not callable(serial.Serial):
        raise RuntimeError("serial.Serial 不可调用；请安装 pyserial，而非 serial 包")
    from serial.tools import list_ports
    if not callable(list_ports.comports):
        raise RuntimeError("pyserial 缺少 list_ports.comports")
except Exception as exc:
    print(str(exc), file=sys.stderr)
    sys.exit(1)
PY
)

# A system interpreter must not load Conda's Python modules or Qt plugins.
clean_env=(env -u PYTHONPATH -u PYTHONHOME -u QT_PLUGIN_PATH
    -u QT_QPA_PLATFORM_PLUGIN_PATH -u QT_QPA_FONTDIR)
check_python() {
    local interpreter=$1 isolated=$2
    if [[ "$isolated" == 1 ]]; then
        "${clean_env[@]}" "$interpreter" -c "$dependency_check" 2>&1
    else
        "$interpreter" -c "$dependency_check" 2>&1
    fi
}
installation_hint() {
    local interpreter=$1
    printf '请在该环境安装 PyQt5 和 pyserial（串口依赖包名为 pyserial）：\n  ' >&2
    printf '%q -m pip install -r %q\n' "$interpreter" "$tool_dir/requirements.txt" >&2
    printf '若 serial 包覆盖了 pyserial，请先卸载同名包并恢复串口库：\n  ' >&2
    printf '%q -m pip uninstall serial\n  ' "$interpreter" >&2
    printf '%q -m pip install --force-reinstall --no-deps pyserial==3.5\n' "$interpreter" >&2
}

if [[ -n "${OMNIPICKER_PYTHON:-}" ]]; then
    interpreter=$(command -v -- "$OMNIPICKER_PYTHON" || true)
    if [[ -z "$interpreter" || ! -x "$interpreter" ]]; then
        printf '指定的 Python 不存在或不可执行：%s\n' "$OMNIPICKER_PYTHON" >&2
        exit 1
    fi
    if ! problem=$(check_python "$interpreter" 0); then
        printf '指定的 Python 依赖检查失败：%s\n%s\n' "$interpreter" "$problem" >&2
        installation_hint "$interpreter"
        exit 1
    fi
    exec "$interpreter" "$tool_dir/omnipicker_serial.py" "$@"
fi

if [[ -e "$tool_dir/.venv" ]]; then
    interpreter="$tool_dir/.venv/bin/python"
    if [[ ! -x "$interpreter" ]]; then
        printf '工具虚拟环境不完整，找不到可执行 Python：%s\n' "$interpreter" >&2
        printf '请重建工具虚拟环境：\n  python3 -m venv %q\n' "$tool_dir/.venv" >&2
        installation_hint "$interpreter"
        exit 1
    fi
    if ! problem=$(check_python "$interpreter" 0); then
        printf '工具虚拟环境依赖检查失败：%s\n%s\n' "$interpreter" "$problem" >&2
        installation_hint "$interpreter"
        exit 1
    fi
    exec "$interpreter" "$tool_dir/omnipicker_serial.py" "$@"
fi

current_python=$(command -v python3 || true)
current_problem='PATH 中没有 python3'
if [[ -n "$current_python" ]]; then
    if current_problem=$(check_python "$current_python" 0); then
        exec "$current_python" "$tool_dir/omnipicker_serial.py" "$@"
    fi
fi

# Retry a system interpreter in a clean environment even if PATH selected that
# same binary: the failed check may have been caused by PYTHONPATH/PYTHONHOME.
declare -A checked=()
fallback_problems=''
for interpreter in /usr/bin/python3 /usr/local/bin/python3; do
    [[ -x "$interpreter" ]] || continue
    canonical=$(readlink -f -- "$interpreter")
    [[ -n "${checked[$canonical]:-}" ]] && continue
    checked[$canonical]=1
    if problem=$(check_python "$interpreter" 1); then
        printf '当前 Python 依赖不可用，改用 %s（已隔离 Python / Qt 环境变量）。\n' "$interpreter" >&2
        exec "${clean_env[@]}" "$interpreter" "$tool_dir/omnipicker_serial.py" "$@"
    fi
    fallback_problems+="$interpreter：$problem"$'\n'
done

printf '没有找到同时具备 PyQt5 和完整 pyserial 的 Python 环境。\n当前环境：%s\n%s\n%s' \
    "${current_python:-未找到}" "$current_problem" "$fallback_problems" >&2
printf '建议创建本工具独立环境：\n  python3 -m venv %q\n' "$tool_dir/.venv" >&2
installation_hint "$tool_dir/.venv/bin/python"
exit 1
