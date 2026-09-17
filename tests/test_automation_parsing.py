"""``app.order.runner.parse_meal_rows`` 的回归锁。

它把订单表格里的「商品名 + 数量」解析成 ``MealInfo``：**总餐次、经济/豪华、份数**
全靠它。解析错了会直接写错排单表的餐数与档次。纯数据、不碰网络，测试零风险。

（原先同文件里的 ``extract_product_note_text`` 测试已随浏览器模式一起移除：
那个函数从 Playwright 页面抓备注，纯接口模式下用不到。）
"""
from __future__ import annotations

import pytest

from app.order.runner import parse_meal_rows


def _rows(*pairs):
    return [{"product": product, "qty": qty} for product, qty in pairs]


# ----------------------------------------------------------------------
# parse_meal_rows：按餐别筛选
# ----------------------------------------------------------------------
def test_only_rows_of_the_requested_meal_type_are_returned():
    rows = _rows(("经济（午餐）", "x1"), ("豪华（晚餐）", "x1"))
    lunch = parse_meal_rows(rows, "午餐")
    dinner = parse_meal_rows(rows, "晚餐")

    assert [m.meal_type for m in lunch] == ["午餐"]
    assert [m.meal_type for m in dinner] == ["晚餐"]


def test_rows_without_any_meal_label_are_ignored():
    """商品名里没有（午餐）/（晚餐）标记时不该产出任何条目。"""
    assert parse_meal_rows(_rows(("随便一个商品", "x3")), "午餐") == []
    assert parse_meal_rows([], "午餐") == []


def test_missing_product_or_qty_keys_do_not_crash():
    assert parse_meal_rows([{}], "午餐") == []
    assert parse_meal_rows([{"qty": "x2"}], "午餐") == []


# ----------------------------------------------------------------------
# parse_meal_rows：总餐次 / 档次 / 份数
# ----------------------------------------------------------------------
@pytest.mark.parametrize("product,expected_meals", [
    ("豪华六餐（午餐）", 6),
    ("经济六餐（午餐）", 6),
    ("单点（午餐）", 1),
    ("普通套餐（午餐）", None),
    # 下面两条专治「判定放宽」：含「六」但不是「六餐」、含「单」但不是「单点」。
    # 少了它们，把判定写成 `"六" in segment` / `"单" in segment` 也能蒙混过关。
    ("六号套餐（午餐）", None),
    ("单纯套餐（午餐）", None),
])
def test_total_meals_is_read_from_the_segment_before_the_label(product, expected_meals):
    got = parse_meal_rows(_rows((product, "x1")), "午餐")
    assert [m.total_meals for m in got] == [expected_meals]


def test_label_with_empty_leading_segment_yields_no_entry():
    """``"（午餐）"`` 前面是空片段 → 直接跳过，不产出条目。"""
    assert parse_meal_rows(_rows(("（午餐）", "x1")), "午餐") == []


@pytest.mark.parametrize("product,expected_grade", [
    ("经济餐（午餐）", "经济"),
    ("豪华套餐（午餐）", "豪华"),
    ("经济豪华（午餐）", "经济"),      # 先判经济
    ("普通套餐（午餐）", None),
    # 含「经」但不是「经济」→ 必须判为 None；防止判定放宽成 `"经" in segment`
    ("经典型套餐（午餐）", None),
    # 含「豪」但不是「豪华」→ 同理
    ("豪享套餐（午餐）", None),
])
def test_grade_is_read_from_the_segment_before_the_label(product, expected_grade):
    got = parse_meal_rows(_rows((product, "x1")), "午餐")
    assert [m.grade for m in got] == [expected_grade]


@pytest.mark.parametrize("qty,expected", [
    ("x3", 3), ("X3", 3), ("x10", 10), ("共 x4 份", 4),
    ("", 1), ("x", 1), ("没有数量", 1),
])
def test_count_comes_from_xN_in_qty_and_defaults_to_one(qty, expected):
    got = parse_meal_rows(_rows(("经济（午餐）", qty)), "午餐")
    assert [m.count for m in got] == [expected]


def test_grade_and_meals_after_the_label_are_not_picked_up():
    """标签**之后**的文字属于下一个片段 —— 不能被算进当前条目。

    这是 ``REG_MEAL_SPLIT`` + ``segments[:-1]`` 的既有语义：
    ``"（午餐）豪华"`` 的「豪华」落在最后一段，会被丢弃 → 不产出条目。
    """
    assert parse_meal_rows(_rows(("（午餐）豪华", "x1")), "午餐") == []


def test_one_row_with_several_labels_yields_one_entry_each():
    """一行里出现多个（午餐）时，每段各产一条，且共享同一个份数。"""
    got = parse_meal_rows(_rows(("豪华（午餐）经济（午餐）六餐（午餐）", "x7")), "午餐")
    assert [(m.total_meals, m.grade, m.count) for m in got] == [
        (None, "豪华", 7), (None, "经济", 7), (6, None, 7)]


def test_empty_segments_between_labels_are_skipped():
    """连续两个标签之间的空片段不产出条目。"""
    got = parse_meal_rows(_rows(("经济（午餐）（午餐）", "x2")), "午餐")
    assert [(m.grade, m.count) for m in got] == [("经济", 2)]


def test_multiple_rows_are_preserved_in_order():
    got = parse_meal_rows(_rows(("豪华（午餐）", "x1"),
                                ("经济（午餐）", "x2"),
                                ("豪华（午餐）", "x3")), "午餐")
    assert [(m.grade, m.count) for m in got] == [("豪华", 1), ("经济", 2), ("豪华", 3)]
