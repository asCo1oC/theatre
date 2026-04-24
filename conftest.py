# Добавляем корень проекта в sys.path, чтобы pytest находил пакет
# ogatt_booker без предварительной установки через pip install -e .
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
