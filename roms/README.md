# roms/

Put your own GBA ROM files here (`.gba`, or `.zip`/`.7z` archives).
`./start.py` picks the only ROM in this folder, or the one whose file name
contains the text you pass (`./start.py emerald`).

ROM files in this folder are ignored by git and never committed.
Battery saves (`.sav`) and save states (`.ss1`) are written next to the ROM.
