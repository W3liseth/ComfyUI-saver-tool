# Opens the Windows "Select folder" dialog and prints the chosen folder (nothing when cancelled).
# Usage: python folder_dialog.py <title> <start folder>
# Runs in its own process so a dialog problem can never take ComfyUI down.
import ctypes
import sys
import uuid
from ctypes import wintypes

CLSID_FILE_OPEN_DIALOG = "DC1C5A9C-E88A-4DDE-A5A1-60F82A20AEF7"
IID_FILE_OPEN_DIALOG = "D57C7288-D4AD-4768-BE02-9D969532D960"
IID_SHELL_ITEM = "43826D1E-E718-42EE-BC55-A1E261C37BFE"
FOS_PICKFOLDERS = 0x20
FOS_FORCEFILESYSTEM = 0x40
SIGDN_FILESYSPATH = 0x80058000
HRESULT_CANCELLED = 0x800704C7
WS_EX_TOPMOST = 0x8
WS_POPUP = 0x80000000

# Vtable slots of IFileOpenDialog (IUnknown, IModalWindow, IFileDialog) and IShellItem.
SHOW, SET_OPTIONS, GET_OPTIONS, SET_FOLDER, SET_TITLE, GET_RESULT = 3, 9, 10, 12, 17, 20
GET_DISPLAY_NAME = 5


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]


def guid(text):
    return GUID.from_buffer_copy(uuid.UUID(text).bytes_le)


def com(obj, index, argtypes, *args):
    vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    return ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)(vtable[index])(obj, *args)


def check(hr):
    if hr < 0:
        raise OSError(f"Windows error {hr & 0xFFFFFFFF:#010x}")


def pick_folder(title, start):
    ole32, user32, shell32 = ctypes.windll.ole32, ctypes.windll.user32, ctypes.windll.shell32
    user32.SetProcessDPIAware()
    check(ole32.CoInitializeEx(None, 2))  # COINIT_APARTMENTTHREADED

    dialog = ctypes.c_void_p()
    check(ole32.CoCreateInstance(ctypes.byref(guid(CLSID_FILE_OPEN_DIALOG)), None, 1, ctypes.byref(guid(IID_FILE_OPEN_DIALOG)), ctypes.byref(dialog)))
    options = wintypes.DWORD()
    check(com(dialog, GET_OPTIONS, [ctypes.POINTER(wintypes.DWORD)], ctypes.byref(options)))
    check(com(dialog, SET_OPTIONS, [wintypes.DWORD], options.value | FOS_PICKFOLDERS | FOS_FORCEFILESYSTEM))
    check(com(dialog, SET_TITLE, [wintypes.LPCWSTR], title))

    shell32.SHCreateItemFromParsingName.argtypes = [wintypes.LPCWSTR, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    folder = ctypes.c_void_p()
    if shell32.SHCreateItemFromParsingName(start, None, ctypes.byref(guid(IID_SHELL_ITEM)), ctypes.byref(folder)) == 0:
        check(com(dialog, SET_FOLDER, [ctypes.c_void_p], folder))

    # The dialog belongs to a background process, so it is owned by an invisible topmost window to open above the browser.
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                       ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                       wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
    owner = user32.CreateWindowExW(WS_EX_TOPMOST, "STATIC", None, WS_POPUP, 0, 0, 0, 0, None, None, None, None)

    hr = com(dialog, SHOW, [wintypes.HWND], owner)
    if hr & 0xFFFFFFFF == HRESULT_CANCELLED:
        return ""
    check(hr)
    item = ctypes.c_void_p()
    check(com(dialog, GET_RESULT, [ctypes.POINTER(ctypes.c_void_p)], ctypes.byref(item)))
    name = ctypes.c_wchar_p()
    check(com(item, GET_DISPLAY_NAME, [wintypes.DWORD, ctypes.POINTER(ctypes.c_wchar_p)], SIGDN_FILESYSPATH, ctypes.byref(name)))
    path = name.value
    ole32.CoTaskMemFree(name)
    return path


if __name__ == "__main__":
    sys.stdout.buffer.write(pick_folder(sys.argv[1], sys.argv[2]).encode("utf-8"))
