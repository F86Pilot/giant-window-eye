"""Hide the mouse pointer on the Pi kiosk.

Run once on the Pi:  python3 invisible-cursor.py
It makes an "invisible" pointer theme and selects it in
~/.config/labwc/environment. Takes effect after a restart.
To get the pointer back, delete the XCURSOR_THEME line from that file.
"""
import os
import struct

d = os.path.expanduser("~/.local/share/icons/invisible/cursors")
os.makedirs(d, exist_ok=True)

# A 1x1 fully transparent cursor in the Xcursor file format.
CHUNK = 0xfffd0002
data = struct.pack("<4sIII", b"Xcur", 16, 0x10000, 1)            # file header, 1 entry
data += struct.pack("<III", CHUNK, 1, 28)                         # table of contents
data += struct.pack("<IIIIIIIII", 36, CHUNK, 1, 1, 1, 1, 0, 0, 0)  # image header: 1x1
data += struct.pack("<I", 0)                                      # one clear pixel
with open(os.path.join(d, "blank"), "wb") as f:
    f.write(data)

names = """default left_ptr arrow top_left_arrow pointer hand1 hand2 pointing_hand text xterm ibeam
wait watch progress left_ptr_watch crosshair cross move fleur grab grabbing all-scroll
not-allowed no-drop help question_arrow context-menu cell copy alias vertical-text
col-resize row-resize n-resize s-resize e-resize w-resize ne-resize nw-resize se-resize sw-resize
ew-resize ns-resize nesw-resize nwse-resize sb_h_double_arrow sb_v_double_arrow
top_side bottom_side left_side right_side top_left_corner top_right_corner
bottom_left_corner bottom_right_corner zoom-in zoom-out""".split()
for n in names:
    p = os.path.join(d, n)
    if not os.path.lexists(p):
        os.symlink("blank", p)

with open(os.path.expanduser("~/.local/share/icons/invisible/index.theme"), "w") as f:
    f.write("[Icon Theme]\nName=invisible\nComment=No visible mouse pointer (Giant Eye kiosk)\n")

env = os.path.expanduser("~/.config/labwc/environment")
lines = open(env).read().splitlines() if os.path.exists(env) else []
lines = [l for l in lines if not l.startswith("XCURSOR_THEME=")]
lines += ["", "# Giant Eye kiosk: hide the mouse pointer. Delete these lines to get it back.",
          "XCURSOR_THEME=invisible"]
with open(env, "w") as f:
    f.write("\n".join(lines) + "\n")
print("Pointer hidden after the next restart.")
