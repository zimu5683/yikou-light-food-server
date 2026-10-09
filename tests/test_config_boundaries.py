"""``app.core.config`` 的边界语义回归锁（由模糊测试发现并固定下来）。

**这份文件的由来**：第 15 轮做「边界验证」时，对配置做了一次 300 组随机
`save → load` 往返 + 8700 次字段比对，发现若干字段**写进去和读回来不一样**。
逐条核对源码后确认：这些**都不是缺陷，而是 `AppConfig.__init__` 里有意的
falsy 兜底**（`x or "默认值"`）。但它们足够反直觉 —— 用户把「商品名称」清空保存，
重启后会**变回「轻食」** —— 所以必须把实际语义钉死，免得日后有人「顺手改成保留空值」
而悄悄改变了既定行为。

模糊测试的另一个结果是**幂等性全部成立**（归一化函数跑两遍与跑一遍相同），
这部分也一并作为回归锁留下。
"""
from __future__ import annotations

import json

import pytest

from app.core.config import (DEFAULTS_REVISION, AppConfig, default_wps_address_order,
                        normalize_wps_address_order, normalize_wps_tables,
                        normalize_wps_test_tables)

# 这些字段在 __init__ 里写成 `x or "默认值"`，因此**空串会被默认值取代**
FALSY_FALLBACKS = [
    ("sss_product_name", "轻食"),
    ("sss_common_address", "嗯哼"),
    ("sss_store_name", "一口轻食"),
    ("sss_fixed_area_code", "330110"),
    ("sss_fixed_address_detail", "浙江农林大学东湖校区"),
]


# ----------------------------------------------------------------------
# sss_order_source：只认 excel，其余一律云端模式
# ----------------------------------------------------------------------
@pytest.mark.parametrize("given,expected", [
    ("excel", "excel"), ("EXCEL", "excel"), (" excel ", "excel"), ("Excel", "excel"),
    ("excel2", "wps"), ("wps", "wps"), ("", "wps"), (None, "wps"), ("e", "wps"),
])
def test_sss_order_source_only_accepts_excel(given, expected):
    """坏值/旧配置缺字段都必须退回云端模式，绝不能变成「读本地 Excel」。"""
    assert AppConfig(sss_order_source=given).sss_order_source == expected


# ----------------------------------------------------------------------
# 空值 → 默认值（有意的 falsy 兜底）
# ----------------------------------------------------------------------
@pytest.mark.parametrize("field,default", FALSY_FALLBACKS)
def test_empty_string_falls_back_to_the_builtin_default(field, default):
    assert getattr(AppConfig(**{field: ""}), field) == default
    assert getattr(AppConfig(**{field: None}), field) == default
    assert getattr(AppConfig(**{field: "自定义"}), field) == "自定义"


@pytest.mark.parametrize("field,default", FALSY_FALLBACKS)
def test_empty_survives_a_save_load_roundtrip_as_the_default(field, default, tmp_path):
    """「清空后重启会变回默认值」这条实际行为要留住。

    若哪天有人把它改成「保留空串」，这条会失败 —— 那时应当先确认这是有意的行为变更。
    """
    path = tmp_path / "config.json"
    AppConfig(**{field: ""}).save(str(path))
    assert getattr(AppConfig.load(str(path)), field) == default


# ----------------------------------------------------------------------
# 空白**不**被裁剪（与 order_date 不一致，属既有行为）
# ----------------------------------------------------------------------
@pytest.mark.parametrize("field", ["sss_product_name", "sss_common_address",
                                   "sss_fixed_address_detail", "phone_number",
                                   "target_url", "sss_account"])
def test_surrounding_whitespace_is_trimmed(field):
    """**用户已确认的行为变更**：这些字段现在会自动去掉首尾空格。

    原先是「只做 falsy 判断、不 strip」，于是 ``" 138 "`` 会被原样保留 —— 而
    ``phone_number`` / ``sss_account`` 同时是**系统密钥链里的账号名**，
    带空格的写法与不带空格会被当成两个不同账号，导致「密码明明存过却取不到」。
    """
    assert getattr(AppConfig(**{field: "  x  "}), field) == "x"


@pytest.mark.parametrize("field", ["sss_product_name", "sss_common_address",
                                   "sss_fixed_address_detail"])
def test_whitespace_only_falls_back_to_the_default(field):
    """纯空白现在等于「没填」→ 回落到默认值（而不是把空格提交上去）。"""
    assert getattr(AppConfig(**{field: "   "}), field) == dict(FALSY_FALLBACKS)[field]


@pytest.mark.parametrize("field", ["phone_number", "target_url", "sss_account"])
def test_whitespace_only_becomes_empty_for_plain_text_fields(field):
    """这几个没有默认值可回落，去完空格就是空串。"""
    assert getattr(AppConfig(**{field: "   "}), field) == ""


def test_order_date_is_also_trimmed():
    """``order_date`` 一直就是 ``str(x or "").strip()``。

    （早先这里写的是「唯一会裁剪的字段」；用户确认的行为变更之后，电话号码、账号、
    商品名、常用地址、固定地址详情等也一并去空格了，所以它不再是特例。）
    """
    assert AppConfig(order_date="  2026-09-16  ").order_date == "2026-09-16"
    assert AppConfig(order_date=None).order_date == ""


# ----------------------------------------------------------------------
# 数值字段的夹紧
# ----------------------------------------------------------------------
@pytest.mark.parametrize("given,expected", [
    (0, 1), (-5, 1), (1, 1), (4, 4), (20, 20), (99, 20),
    (None, 4), ("abc", 4), ("7", 7),
])
def test_sss_max_workers_is_clamped(given, expected):
    # 读不出来时回落到**出厂默认 4**（v2 起）；显式 1/2/6/12 等都原样保留，
    # 只有「旧配置里等于上一代出厂默认的 8」才由迁移搬走（见下面的迁移用例）。
    assert AppConfig(sss_max_workers=given).sss_max_workers == expected


@pytest.mark.parametrize("given,expected", [
    (0, 1.0), (-3, 1.0), (20.0, 20.0), (500, 120.0), (None, 30.0), ("abc", 30.0),
])
def test_sss_read_timeout_is_clamped(given, expected):
    assert AppConfig(sss_read_timeout_s=given).sss_read_timeout_s == expected


@pytest.mark.parametrize("given,expected", [
    (0, 0.0), (-5, 0.0), (2.5, 2.5), (10, 10.0), (999, 60.0),
    (None, 2.5), ("abc", 2.5), ("3.5", 3.5),
])
def test_sss_submit_min_interval_is_clamped(given, expected):
    # 出厂 2.5 秒（平台建单受理节奏约 1 单 / 2.1 秒 + 余量）；显式 0 = 关闭节流。
    assert AppConfig(sss_submit_min_interval_s=given).sss_submit_min_interval_s == expected


def test_sss_submit_min_interval_nan_falls_back_to_default():
    assert AppConfig(sss_submit_min_interval_s=float("nan")).sss_submit_min_interval_s == 2.5


def test_legacy_config_without_the_interval_gets_the_paced_default(tmp_path):
    """旧 config.json 没有 sss_submit_min_interval_s：加载即按出厂默认 2.5 秒节流。"""
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"sss_max_workers": 4,
                                "defaults_revision": DEFAULTS_REVISION},
                               ensure_ascii=False), encoding="utf-8")
    loaded = AppConfig.load(path)
    assert loaded.sss_submit_min_interval_s == 2.5
    loaded.save()
    assert AppConfig.load(path).sss_submit_min_interval_s == 2.5


def test_explicit_zero_interval_survives_a_save_load_roundtrip(tmp_path):
    """0 = 显式关闭节流，保存后不能被出厂默认覆盖回 2.5。"""
    path = tmp_path / "config.json"
    AppConfig(sss_submit_min_interval_s=0.0).save(path)
    assert AppConfig.load(path).sss_submit_min_interval_s == 0.0


# ----------------------------------------------------------------------
# 出厂默认迁移：v0 → v1（并发 4 → 8、读取超时 20 → 30 秒）
#           v1 → v2（并发 8 → 4、读取超时 30 秒不变）
# ----------------------------------------------------------------------
def test_legacy_factory_defaults_are_migrated_once():
    """旧配置里等于旧出厂默认的取值搬一次；用户显式改过的取值一律不动。"""
    # v1 时代写下的配置：并发就是那一代的出厂默认 8 → 搬到 4。
    migrated = AppConfig(defaults_revision=1, sss_max_workers=8, sss_read_timeout_s=20.0)
    assert migrated.sss_max_workers == 4
    assert migrated.sss_read_timeout_s == 30.0
    assert migrated.defaults_revision == DEFAULTS_REVISION

    # v0 的老配置（那一代出厂默认也是 4）：终值仍是 4，读取超时 20 → 30。
    oldest = AppConfig(defaults_revision=0, sss_max_workers=4, sss_read_timeout_s=20.0)
    assert oldest.sss_max_workers == 4
    assert oldest.sss_read_timeout_s == 30.0

    kept = AppConfig(defaults_revision=1, sss_max_workers=2, sss_read_timeout_s=45.0)
    assert kept.sss_max_workers == 2
    assert kept.sss_read_timeout_s == 45.0


def test_current_revision_never_rewrites_explicit_values():
    """迁移是一次性的：已经是当前版本的配置里，8 / 20.0 就是用户的真实选择。"""
    current = AppConfig(defaults_revision=DEFAULTS_REVISION, sss_max_workers=8,
                        sss_read_timeout_s=20.0)
    assert current.sss_max_workers == 8
    assert current.sss_read_timeout_s == 20.0


def test_load_migrates_an_old_file_without_the_revision_key(tmp_path):
    """旧 config.json 没有 defaults_revision 键 = 迁移前：加载即按新默认生效并写回。"""
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"sss_max_workers": 8, "sss_read_timeout_s": 20.0},
                               ensure_ascii=False), encoding="utf-8")

    loaded = AppConfig.load(path)
    assert loaded.sss_max_workers == 4
    assert loaded.sss_read_timeout_s == 30.0
    assert loaded.defaults_revision == DEFAULTS_REVISION

    loaded.save()
    assert AppConfig.load(path).sss_max_workers == 4


def test_explicit_transport_values_survive_a_save_load_roundtrip(tmp_path):
    path = tmp_path / "config.json"
    AppConfig(sss_max_workers=6, sss_read_timeout_s=45.0).save(path)
    back = AppConfig.load(path)
    assert back.sss_max_workers == 6
    assert back.sss_read_timeout_s == 45.0


def test_split_ratio_is_clamped_on_construction():
    from app.core.config import MAX_SPLIT_RATIO, MIN_SPLIT_RATIO

    assert AppConfig(split_ratio=0).split_ratio == MIN_SPLIT_RATIO
    assert AppConfig(split_ratio=9).split_ratio == MAX_SPLIT_RATIO


# ----------------------------------------------------------------------
# save → load 往返：**合法**取值必须逐字段保真
# ----------------------------------------------------------------------
ROUNDTRIP_FIELDS = {
    "target_url": "https://m.icall.me/admin/#/login",
    "phone_number": "13800000000",
    "order_date": "2026-09-16",
    "order_count": 42,
    "sss_url": "https://sss.example/takeout",
    "sss_account": "sss-user",
    "sss_order_source": "excel",
    "sss_product_name": "轻食套餐",
    "sss_common_address": "东湖校区",
    "sss_use_fixed_address": True,
    "sss_fixed_lnt": 119.72,
    "sss_fixed_lat": 30.23,
    "sss_fixed_area_code": "330110",
    "sss_fixed_address_detail": "浙江农林大学东湖校区",
    "sss_dry_run": False,
    "sss_preflight": True,
    "sss_max_workers": 6,
    "sss_read_timeout_s": 45.0,
    "sss_idempotency_field": "client_request_id",
    "wps_enabled": True,
    "wps_test_mode": False,
    "wps_test_file_id": "TEST_FID",
    "wps_drive_id": "DRIVE",
    "wps_cli_path": "/usr/local/bin/kdocs-cli",
    "wps_target_hour_start": 20,
    "wps_target_hour_end": 10,
    "wps_marker_enabled": False,
    "wps_sort_enabled": True,
}


def test_every_field_survives_a_save_load_roundtrip(tmp_path):
    path = tmp_path / "config.json"
    cfg = AppConfig()
    for field, value in ROUNDTRIP_FIELDS.items():
        setattr(cfg, field, value)

    cfg.save(str(path))
    back = AppConfig.load(str(path))

    for field, value in ROUNDTRIP_FIELDS.items():
        assert getattr(back, field) == value, f"{field} 往返后变了"


def test_order_count_none_survives_roundtrip(tmp_path):
    """``order_count=None`` 表示「全部订单」，不能被当成 0 或丢失。"""
    path = tmp_path / "config.json"
    AppConfig(order_count=None).save(str(path))
    assert AppConfig.load(str(path)).order_count is None


# ----------------------------------------------------------------------
# 归一化幂等性（模糊测试确认成立，这里固化成回归锁）
# ----------------------------------------------------------------------
def test_normalizers_are_idempotent_on_messy_input():
    """归一化函数跑两遍必须与跑一遍相同 —— 否则反复读写会让配置持续漂移。

    注意本文件只覆盖**幂等性**；「丢掉没有 file_id 的条目」这条语义由
    ``test_config_normalization.py`` 负责（变异测试确认：把那个过滤去掉时，
    本文件仍然通过，而那边有 2 条会失败）。两者互补，不要误以为本文件管了全部。
    """
    messy_tables = {"  东湖中餐  ": "  F1  ", "": "X", 42: "Y",
                    "衣锦中餐": {"file_id": "", "drive_id": " D "},
                    "幽灵": {"file_id": ""}, "医学院晚餐": None}
    once = normalize_wps_tables(messy_tables)
    assert normalize_wps_tables(once) == once

    messy_order = {"  衣锦中餐  ": "外卖柜\n\n  校门口 \n", "医学院中餐": [],
                   "东湖中餐": 42, "": ["x"]}
    once = normalize_wps_address_order(messy_order)
    assert normalize_wps_address_order(once) == once

    once = normalize_wps_test_tables({"东湖中餐": " F1 ", "衣锦中餐": "", "": "X"})
    assert normalize_wps_test_tables(once) == once


def test_normalizers_never_mutate_their_input():
    """归一化可以返回新对象，但不能就地改动调用方传进来的字典。"""
    source = {"东湖中餐": {"file_id": "F1"}}
    snapshot = {"东湖中餐": {"file_id": "F1"}}
    normalize_wps_tables(source)
    normalize_wps_address_order(source)
    normalize_wps_test_tables(source)
    assert source == snapshot


def test_address_order_defaults_are_stable_across_calls():
    """出厂顺序必须稳定，否则界面「恢复默认」两次给的结果会不同。"""
    assert default_wps_address_order() == default_wps_address_order()
