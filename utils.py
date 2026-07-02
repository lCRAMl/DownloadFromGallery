# utils.py
from pathlib import Path
import sys


def resource_path(relative_path):
    """
    Liefert den korrekten Pfad zu einer Ressource, auch wenn das Programm
    als PyInstaller onefile gebündelt wurde.
    """
    if getattr(sys, "frozen", False):
        # temporärer Ordner, in den PyInstaller extrahiert
        base_path = Path(sys._MEIPASS)
    else:
        base_path = Path(__file__).parent

    return base_path / relative_path