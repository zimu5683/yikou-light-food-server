"""delivery_point 地址归一化单元测试。

覆盖 2026-04~06 真实订单里出现的代表性写法，防止规则回归。
"""
from __future__ import annotations

import pytest

from app.order.delivery import (
    detect_campus,
    get_donghu_delivery_mode,
    normalize_delivery_point as ndp,
    normalize_donghu_delivery_point as nddp,
)

# (原文, 期望点名, 期望置信度, 期望校区)
CASES = [
    # 东湖字母楼：各种后缀/杂讯
    ("浙江省杭州市临安区浙江农林大学(东湖校区) A5 506", "A5", "high", "东湖农林"),
    ("浙江省杭州市临安区浙江农林大学(东湖校区) A5宿舍楼", "A5", "high", "东湖农林"),
    ("浙江省杭州市临安区浙江农林大学(东湖校区) A6 603", "A6", "high", "东湖农林"),
    ("浙江省杭州市临安区浙江农林大学(东湖校区) A6寝室楼", "A6", "high", "东湖农林"),
    ("浙江省杭州市临安区浙江农林大学(东湖校区) B5号楼", "b5", "high", "东湖农林"),
    ("浙江省杭州市临安区浙江农林大学(东湖校区) b5", "b5", "high", "东湖农林"),
    ("浙江省杭州市临安区浙江农林大学(东湖校区) a5 518", "A5", "high", "东湖农林"),
    ("浙江省杭州市临安区浙江农林大学(东湖校区) B11宿舍楼", "b11", "high", "东湖农林"),
    ("浙江省杭州市临安区浙江农林大学(东湖校区) C5号寝室楼", "C5", "high", "东湖农林"),
    ("浙江省杭州市临安区浙江农林大学(东湖校区) d2宿舍", "D2", "high", "东湖农林"),
    ("浙江省杭州市临安区浙江农林大学(东湖校区) D2学生公寓", "D2", "high", "东湖农林"),
    ("浙江省杭州市临安区浙江农林大学(东湖校区) D1楼下美团外卖柜", "D1", "high", "东湖农林"),
    ("浙江省杭州市临安区浙江农林大学(东湖校区) b1外卖柜", "b1", "high", "东湖农林"),
    ("浙江省杭州市临安区浙江农林大学(东湖校区) A6宿舍楼下外卖柜", "A6", "high", "东湖农林"),
    ("浙江省杭州市临安区浙江农林大学(东湖校区) B9号楼 GUUU+13705820763+B9寝室楼", "b9", "high", "东湖农林"),
    ("浙江省杭州市临安区浙江農林大學(東湖校區) D2宿舍楼下", "D2", "high", "东湖农林"),
    ("浙江省杭州市临安区浙江农林大学东湖校区B2号楼 b1-2外卖柜", "b2", "medium", "东湖农林"),
    # 楼码与校门并存 → 按口径放宿舍楼下取楼码
    ("浙江省杭州市临安区浙江农林大学(东湖校区) C7栋502(大西门)", "C7", "high", "东湖农林"),
    ("浙江省杭州市临安区浙江农林大学(东湖校区) 小西门A6", "A6", "high", "东湖农林"),
    ("浙江省杭州市临安区浙江农林大学(东湖校区) A5 605（小西门）", "A5", "high", "东湖农林"),
    # 校门短名
    ("浙江省杭州市临安区浙江农林大学(东湖校区) 大西门外卖柜", "大西", "high", "东湖农林"),
    ("浙江省杭州市临安区浙江农林大学(东湖校区)-西南1门 小西门外卖柜", "小", "high", "东湖农林"),
    ("浙江省杭州市临安区浙江农林大学(东湖校区) 小西门", "小", "high", "东湖农林"),
    ("浙江省杭州市临安区浙江农林大学(东湖校区) B6", "b6", "high", "东湖农林"),
    ("浙江省杭州市临安区浙江农林大学(东湖校区) 大西门", "大西", "high", "东湖农林"),
    ("浙江省杭州市临安区浙江农林大学(东湖校区) 西门", "大西", "high", "东湖农林"),
    ("浙江省杭州市临安区浙江农林大学(东湖校区)-西门 外卖柜", "大西", "high", "东湖农林"),
    # 命名点位
    ("浙江省杭州市临安区浙江农林大学(东湖校区)图书馆 外卖柜", "图书馆", "high", "东湖农林"),
    ("浙江省杭州市临安区浙江农林大学(东湖校区) 国重楼下", "国重楼", "high", "东湖农林"),
    ("浙江省杭州市临安区浙江农林大学(东湖校区) 学三外卖柜", "学三", "high", "东湖农林"),
    # 已确认别名：行政楼放学三外卖柜 = 学三
    ("浙江省杭州市临安区浙江农林大学(东湖校区)行政楼 325 室（放学三外卖柜）", "学三", "high", "东湖农林"),
    # 医学院：数字楼 → 医N号
    ("浙江省杭州市临安区杭州医学院(临安校区) 5号楼", "医 5号", "high", "医学院"),
    ("浙江省杭州市临安区杭州医学院(临安校区) 女寝五号楼", "医 5号", "high", "医学院"),
    ("浙江省杭州市临安区杭州医学院(临安校区) 六号楼", "医 6号", "high", "医学院"),
    ("浙江省杭州市临安区杭州医学院(临安校区) 5号楼214", "医 5号", "high", "医学院"),
    ("浙江省杭州市临安区杭州医学院(临安校区)宿舍楼 5-112", "医 5号", "high", "医学院"),
    ("浙江省杭州市临安区Hangzhou Medical College (Lin 'an Campus) Lin'an District 4号寝室楼", "医 4号", "high", "医学院"),
    # 医学院相邻楼号对：12→1、34→3、56→5、78→7
    ("浙江省杭州市临安区杭州医学院(临安校区) 12号楼", "医 1号", "high", "医学院"),
    ("浙江省杭州市临安区杭州医学院(临安校区) 34号宿舍楼", "医 3号", "high", "医学院"),
    ("浙江省杭州市临安区杭州医学院(临安校区) 56号楼", "医 5号", "high", "医学院"),
    ("浙江省杭州市临安区杭州医学院(临安校区) 78号楼", "医 7号", "high", "医学院"),
    ("浙江省杭州市临安区杭州医学院(临安校区) 3、4号楼", "医 3号", "high", "医学院"),
    ("浙江省杭州市临安区杭州医学院(临安校区) 五六号楼", "医 5号", "high", "医学院"),
    ("浙江省杭州市临安区杭州医学院(临安校区) 杭州医学院(临安校区)三四号楼", "医 3号", "high", "医学院"),
    # D区/D门含糊写法：有楼号→对应D楼；只有D区/D门→默认D2
    ("浙江省杭州市临安区浙江农林大学(东湖校区) D区宿舍楼", "D2", "high", "东湖农林"),
    ("浙江省杭州市临安区浙江农林大学东湖校区D区宿舍楼 外卖柜", "D2", "high", "东湖农林"),
    ("浙江省杭州市临安区浙江农林大学(东湖校区) D门", "D2", "high", "东湖农林"),
    ("浙江省杭州市临安区浙江农林大学(东湖校区) D区1号楼", "D1", "high", "东湖农林"),
    ("浙江省杭州市临安区浙江农林大学(东湖校区) D1D区架空层", "D1", "high", "东湖农林"),
    ("浙江省杭州市临安区浙江农林大学(东湖校区) D区D2", "D2", "high", "东湖农林"),
    ("浙江省杭州市临安区浙江农林大学(东湖校区)-D2宿舍楼 D1", "D2", "high", "东湖农林"),
    ("浙江省杭州市临安区浙江农林大学(东湖校区)-D2宿舍楼 D1宿舍楼外卖柜", "D2", "high", "东湖农林"),
    ("浙江省杭州市临安区浙江农林大学(东湖校区) D1D2", "D2", "high", "东湖农林"),
    ("浙江省杭州市临安区浙江农林大学(东湖校区) D1、D2", "D2", "high", "东湖农林"),
    # 已在排单表里的简短地址：保持原样点
    ("A7", "A7", "high", "未知"),
    ("大西", "大西", "high", "未知"),
]


@pytest.mark.parametrize("address,point,confidence,campus", CASES)
def test_normalize(address, point, confidence, campus):
    res = ndp(address)
    assert res["point"] == point, f"{address}: point={res['point']} cands={res['candidates']}"
    assert res["confidence"] == confidence, f"{address}: conf={res['confidence']} reason={res['reason']}"
    assert res["campus"] == campus, f"{address}: campus={res['campus']}"


def test_campus_detection():
    assert detect_campus("浙江省杭州市临安区浙江农林大学(东湖校区) A5") == "东湖农林"
    assert detect_campus("浙江省杭州市临安区杭州医学院 5号楼") == "医学院"
    assert detect_campus("浙江省杭州市临安区浙江农林大学联建公寓 E2") == "衣锦联建"
    # 杭电信工：中文全称与英文全称两种写法（2026-10-07 实测）。
    assert detect_campus("浙江省杭州市临安区杭州电子科技大学信息工程学院(青山湖校区)") == "杭电信工"
    assert detect_campus(
        "浙江省杭州市临安区School of Information Engineering, Hangzhou Dianzi "
        "University Qingshan Lake Campus Zijin Court Lin'an District") == "杭电信工"
    assert detect_campus("杭州市临安区杭电路1号北门") == "杭电信工"
    assert detect_campus("校门口") == "未知"


def test_unknown_returns_empty_point():
    res = ndp("浙江省杭州市临安区杭州医学院(临安校区) 杭州医学院(临安校区)")
    assert res["confidence"] == "unknown"
    assert res["point"] == ""


def test_alias_override():
    res = ndp("浙江省杭州市临安区浙江农林大学(东湖校区) C'6321", aliases={"浙江省杭州市临安区浙江农林大学(东湖校区) C'6321": "C6"})
    assert res["point"] == "C6"
    assert res["confidence"] == "high"
    assert res["reason"] == "别名表命中"


def test_bare_west_gate_directly_uses_daxi():
    res = ndp("浙江农林大学东湖校区西门")
    assert res["point"] == "大西"
    assert res["confidence"] == "high"
    assert res["reason"] == "唯一候选"


def test_explicit_west_gate_wins_over_duplicated_generic_prefix():
    res = ndp("浙江农林大学东湖校区-西门 大西门外卖柜")
    assert res["point"] == "大西"
    assert res["confidence"] == "high"


# ----------------------------------------------------------------------
# 东湖：小字先分「存柜 / 寝室楼下」，再到地址里找点位（2026-11 店主口径）
# ----------------------------------------------------------------------
def test_donghu_delivery_mode_from_product_note():
    assert get_donghu_delivery_mode("东湖小西门大西门存柜费用") == "存柜"
    assert get_donghu_delivery_mode("东湖小西门大西门存柜费") == "存柜"
    assert get_donghu_delivery_mode("东湖小西门大西门寄存费用") == "存柜"
    assert get_donghu_delivery_mode("东湖校区配送寝室楼下外卖柜") == "楼下"
    assert get_donghu_delivery_mode("东湖配送寝室楼下外卖柜") == "楼下"
    assert get_donghu_delivery_mode("送到楼下") == "楼下"
    assert get_donghu_delivery_mode("") == ""
    assert get_donghu_delivery_mode("联建门口外卖柜") == ""


# (小字, 地址, 期望点位, 期望置信度)
DONGHU_SPEC_CASES = [
    # 存柜单：只写门名 → 门
    ("东湖小西门大西门存柜费用",
     "浙江省杭州市临安区浙江农林大学(东湖校区) 大西门", "大西", "high"),
    ("东湖小西门大西门存柜费",
     "浙江省杭州市临安区浙江农林大学(东湖校区)-西南1门 小西门外卖柜", "小", "high"),
    # 存柜单：楼栋与门名并存 → 按寝室楼记录（不需要写门名）
    ("东湖小西门大西门存柜费用",
     "浙江省杭州市临安区浙江农林大学(东湖校区)-A6号楼 A6418小西门", "A6", "high"),
    # 存柜单：只写寝室楼（没写门名）→ 交人工填写
    ("东湖小西门大西门存柜费用",
     "浙江省杭州市临安区浙江农林大学(东湖校区) A7", "", "unknown"),
    ("东湖小西门大西门存柜费用",
     "浙江省杭州市临安区浙江农林大学(东湖校区) D1宿舍楼", "", "unknown"),
    # 寝室楼下单：地址里的寝室楼/命名取餐点
    ("东湖校区配送寝室楼下外卖柜",
     "浙江省杭州市临安区浙江农林大学(东湖校区) C11", "C11", "high"),
    ("东湖配送寝室楼下外卖柜",
     "浙江省杭州市临安区浙江农林大学(东湖校区) D2宿舍", "D2", "high"),
    ("东湖校区配送寝室楼下外卖柜",
     "浙江省杭州市临安区浙江农林大学(东湖校区)图书馆 外卖柜", "图书馆", "high"),
    # 寝室楼下单：门名与楼栋并存时仍按寝室楼
    ("东湖校区配送寝室楼下外卖柜",
     "浙江省杭州市临安区浙江农林大学(东湖校区) A6号楼(A6418小西门)", "A6", "high"),
    # 寝室楼下单：地址里只有门名/没有寝室楼 → 交人工填写
    ("东湖校区配送寝室楼下外卖柜",
     "浙江省杭州市临安区浙江农林大学(东湖校区) 大西门", "", "unknown"),
    ("东湖校区配送寝室楼下外卖柜",
     "浙江省杭州市临安区浙江农林大学(东湖校区)", "", "unknown"),
]


@pytest.mark.parametrize("note,address,point,confidence", DONGHU_SPEC_CASES)
def test_donghu_spec_driven_point(note, address, point, confidence):
    res = nddp(address, note)
    assert res["point"] == point, f"{address}: {res}"
    assert res["confidence"] == confidence, f"{address}: {res}"


def test_donghu_unknown_spec_falls_back_to_old_address_rule():
    # 小字认不出（空/其它校区文案）时保持旧行为，不改变历史口径。
    address = "浙江省杭州市临安区浙江农林大学(东湖校区) A5"
    for note in ("", "联建门口外卖柜"):
        res = nddp(address, note)
        assert (res["point"], res["confidence"]) == ("A5", "high")


def test_donghu_alias_hit_is_trusted():
    address = "浙江省杭州市临安区浙江农林大学(东湖校区) C'6321"
    res = nddp(address, "东湖小西门大西门存柜费用", aliases={address: "C6"})
    assert (res["point"], res["confidence"]) == ("C6", "high")
    assert res["reason"] == "别名表命中"

