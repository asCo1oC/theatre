"""Тесты выбора соседних мест."""
from ogatt_booker.qt import _find_adjacent_seats


def test_find_adjacent_seats_single():
    """Одно место всегда возвращается как соседнее с собой."""
    seats_info = [
        {"idx": 0, "x": 100, "y": 50, "title": "A1"},
        {"idx": 1, "x": 150, "y": 50, "title": "A2"},
        {"idx": 2, "x": 200, "y": 50, "title": "A3"},
    ]
    idxs, is_adjacent = _find_adjacent_seats(seats_info, 1)
    assert len(idxs) == 1
    assert is_adjacent is True


def test_find_adjacent_seats_adjacent():
    """Соседние места должны быть найдены."""
    seats_info = [
        {"idx": 0, "x": 100, "y": 50, "title": "A1"},
        {"idx": 1, "x": 130, "y": 50, "title": "A2"},
        {"idx": 2, "x": 160, "y": 50, "title": "A3"},
    ]
    idxs, is_adjacent = _find_adjacent_seats(seats_info, 2)
    assert len(idxs) == 2
    assert is_adjacent is True
    assert idxs in ([0, 1], [1, 2])


def test_find_adjacent_seats_prefers_same_row_block_over_mixed_rows():
    """При наличии блока в одном ряду он должен побеждать разрозненные центральные места."""
    seats_info = [
        {"idx": 0, "x": 480, "y": 100, "title": "A10"},
        {"idx": 1, "x": 620, "y": 130, "title": "B11"},
        {"idx": 2, "x": 650, "y": 130, "title": "B12"},
        {"idx": 3, "x": 680, "y": 130, "title": "B13"},
        {"idx": 4, "x": 820, "y": 100, "title": "A20"},
    ]
    idxs, is_adjacent = _find_adjacent_seats(seats_info, 2)
    assert is_adjacent is True
    assert idxs in ([1, 2], [2, 3])


def test_find_adjacent_seats_non_adjacent_fallback():
    """Если соседних мест нет, всё равно вернёт requested количество."""
    seats_info = [
        {"idx": 0, "x": 100, "y": 50, "title": "A1"},
        {"idx": 1, "x": 300, "y": 50, "title": "A5"},
        {"idx": 2, "x": 500, "y": 50, "title": "A10"},
    ]
    idxs, is_adjacent = _find_adjacent_seats(seats_info, 2)
    assert len(idxs) == 2
    assert is_adjacent is False


def test_find_adjacent_seats_different_rows():
    """Места на разных строках не должны объединяться в соседний блок."""
    seats_info = [
        {"idx": 0, "x": 100, "y": 50, "title": "A1"},
        {"idx": 1, "x": 130, "y": 50, "title": "A2"},
        {"idx": 2, "x": 100, "y": 100, "title": "B1"},
        {"idx": 3, "x": 130, "y": 100, "title": "B2"},
    ]
    idxs, is_adjacent = _find_adjacent_seats(seats_info, 2)
    assert len(idxs) == 2
    assert is_adjacent is True
    assert idxs in ([0, 1], [2, 3])
