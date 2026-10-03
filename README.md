# Free Space

A small GTK4 app that shows what is using your disk space and lets you delete it.

```
./freespace.py            # scan your home folder
./freespace.py /var       # scan any folder
./install.sh              # optional: add `freespace` to ~/.local/bin and the app menu
```

Requirements: Python 3.12 or newer, plus PyGObject with GTK 4. On Arch you need `python-gobject` and `gtk4`.

## Using it
- **Locations** sidebar: home, Trash and each mounted disk, with a usage bar. The bar turns amber at 80% full and red at 90%. Click one to scan it.
- The list is sorted biggest-first. **Share of folder** shows each entry's percentage of the folder you are viewing.
- Double-click a folder (or press Enter) to go into it. Use **Backspace** to go up and **Alt+Left** to go back. You can also click a segment of the path bar.
- **Largest files** lists the 500 biggest files anywhere under the current folder.
- Removing: select one or more rows (Ctrl/Shift-click). Then use the buttons, the right-click menu, **Delete** (move to Trash) or **Shift+Delete** (delete permanently). Every removal asks you to confirm first.
  Moving files to the Trash frees no space on that disk. Use *Delete Permanently*, or scan the Trash afterwards and delete from there.

## How sizes are counted
Sizes are space actually allocated on disk (`st_blocks`), the same as `du`. Hard links are counted once and symlinks are not followed. The scan does not cross into other filesystems; those show as "(other filesystem)" and you can double-click one to scan it. Folders you have no permission to read are marked, and their contents are not counted.

## Tests
```
python3 tests/test_model.py   # scanner totals vs `du`, tree updates after removal
python3 tests/test_gui.py     # drives the real window: scan, navigate, delete, trash (needs a display)
```
